import base64
import datetime
import os
import json
import isbnlib
import requests
import time
import urlparse
import urllib
import logging
from PIL import Image
from nose.tools import set_trace
from StringIO import StringIO

from model import (
    get_one_or_create,
    CirculationEvent,
    CoverageProvider,
    Credential,
    DataSource,
    LicensePool,
    Measurement,
    Representation,
    Resource,
    Subject,
    Identifier,
    Edition,
)

from integration import (
    FilesystemCache,
    CoverImageMirror,
)
from monitor import Monitor
from util import LanguageCodes

class OverdriveAPI(object):

    TOKEN_ENDPOINT = "https://oauth.overdrive.com/token"
    PATRON_TOKEN_ENDPOINT = "https://oauth-patron.overdrive.com/patrontoken"

    LIBRARY_ENDPOINT = "http://api.overdrive.com/v1/libraries/%(library_id)s"
    METADATA_ENDPOINT = "http://api.overdrive.com/v1/collections/%(collection_token)s/products/%(item_id)s/metadata"
    EVENTS_ENDPOINT = "http://api.overdrive.com/v1/collections/%(collection_name)s/products?lastupdatetime=%(lastupdatetime)s&sort=%(sort)s&formats=%(formats)s&limit=%(limit)s"
    CHECKOUTS_ENDPOINT = "http://patron.api.overdrive.com/v1/patrons/me/checkouts"
    AVAILABILITY_ENDPOINT = "http://api.overdrive.com/v1/collections/%(collection_name)s/products/%(product_id)s/availability"

    CRED_FILE = "oauth_cred.json"
    BIBLIOGRAPHIC_DIRECTORY = "bibliographic"

    MAX_CREDENTIAL_AGE = 50 * 60

    PAGE_SIZE_LIMIT = 300
    EVENT_SOURCE = "Overdrive"

    EVENT_DELAY = datetime.timedelta(minutes=60)

    # The ebook formats we care about.
    FORMATS = "ebook-epub-open,ebook-epub-adobe,ebook-pdf-adobe,ebook-pdf-open"

    
    def __init__(self, _db):
        self._db = _db
        self.source = DataSource.lookup(_db, DataSource.OVERDRIVE)

        # Set some stuff from environment variables
        self.client_key = os.environ['OVERDRIVE_CLIENT_KEY']
        self.client_secret = os.environ['OVERDRIVE_CLIENT_SECRET']
        self.website_id = os.environ['OVERDRIVE_WEBSITE_ID']
        self.library_id = os.environ['OVERDRIVE_LIBRARY_ID']
        self.collection_name = os.environ['OVERDRIVE_COLLECTION_NAME']

        # Get set up with up-to-date credentials from the API.
        self.check_creds()
        self.collection_token = self.get_library()['collectionToken']

    def check_creds(self):
        """If the Bearer Token has expired, update it."""
        credential = Credential.lookup(
            self._db, DataSource.OVERDRIVE, self.refresh_creds)
        self.token = credential.credential

    def refresh_creds(self, credential):
        """Fetch a new Bearer Token and update the given Credential object."""
        response = self.token_post(
            self.TOKEN_ENDPOINT,
            dict(grant_type="client_credentials"))
        data = response.json()
        credential.credential = data['access_token']
        expires_in = (data['expires_in'] * 0.9)
        credential.expires = datetime.datetime.utcnow() + datetime.timedelta(
            seconds=expires_in)

    def get(self, url, extra_headers, exception_on_401=False):
        """Make an HTTP GET request using the active Bearer Token."""
        headers = dict(Authorization="Bearer %s" % self.token)
        headers.update(extra_headers)
        status_code, headers, content = Representation.simple_http_get(
            url, headers)
        if status_code == 401:
            if exception_on_401:
                # This is our second try. Give up.
                raise Exception("Something's wrong with the OAuth Bearer Token!")
            else:
                # Refresh the token and try again.
                self.check_creds()
                return self.get(url, extra_headers, True)
        else:
            return status_code, headers, content

    def token_post(self, url, payload, headers={}):
        """Make an HTTP POST request for purposes of getting an OAuth token."""
        s = "%s:%s" % (self.client_key, self.client_secret)
        auth = base64.encodestring(s).strip()
        headers = dict(headers)
        headers['Authorization'] = "Basic %s" % auth
        return requests.post(url, payload, headers=headers)

    def get_patron_access_token(self, library_card, pin):
        """Create an OAuth token for the given patron."""
        payload = dict(
            grant_type="password",
            username=library_card,
            password=pin,
            scope="websiteid:%s authorizationname:%s" % (
                self.website_id, "default")
        )
        response = self.token_post(self.PATRON_TOKEN_ENDPOINT, payload)
        if response.status_code == 200:
            access_token = response.json()['access_token']
        else:
            access_token = None
        return access_token, response

    def checkout(self, patron_access_token, overdrive_id, 
                 format_type='ebook-epub-adobe'):
        auth_header = dict(Authorization="Bearer %s" % patron_access_token)
        headers = dict(auth_header)
        headers["Content-Type"] = "application/json"
        payload = dict(fields=[dict(name="reserveId", value=overdrive_id),
                               dict(name="formatType", value=format_type)])
        payload = json.dumps(payload)
        response = requests.post(
            self.CHECKOUTS_ENDPOINT, headers=headers, data=payload)

        set_trace()
        # TODO: We need a better error URL here, not that it matters.
        expires, content_link_gateway = self.extract_data_from_checkout_response(
            response.json(), format_type, "http://library-simplified.com/")

        # Now GET the content_link_gateway, which will point us to the
        # ACSM file or equivalent.
        final_response = requests.get(content_link_gateway, headers=auth_header)
        content_link, content_type = self.extract_content_link(final_response)
        return content_link, content_type, expires

    @classmethod
    def extract_data_from_checkout_response(cls, checkout_response_json,
                                            format_type, error_url):

        expires = datetime.datetime.strptime(
            checkout_response_json['expires'], "%Y-%m-%dT%H:%M:%SZ")
        return expires, cls.get_download_link(
            checkout_response_json, format_type, error_url)


    def extract_content_link(self, content_link_gateway_json):
        link = content_link_gateway_json['links']['contentlink']
        return link['href'], link['type']

    def get_library(self):
        url = self.LIBRARY_ENDPOINT % dict(library_id=self.library_id)
        representation, cached = Representation.get(
            self._db, url, self.get, data_source=self.source)
        return json.loads(representation.content)

    def recently_changed_ids(self, start, cutoff):
        """Get IDs of books whose status has changed between the start time
        and now.
        """
        # `cutoff` is not supported by Overdrive, so we ignore it. All
        # we can do is get events between the start time and now.

        last_update_time = start-self.EVENT_DELAY
        print start, last_update_time
        params = dict(lastupdatetime=last_update_time,
                      formats=self.FORMATS,
                      sort="popularity:desc",
                      limit=self.PAGE_SIZE_LIMIT,
                      collection_name=self.collection_name)
        next_link = self.make_link_safe(self.EVENTS_ENDPOINT % params)
        while next_link:
            page_inventory, next_link = self._get_book_list_page(next_link)
            # We won't be sending out any events for these books yet,
            # because we don't know if anything changed, but we will
            # be putting them on the list of inventory items to
            # refresh. At that point we will send out events.
            for i in page_inventory:
                if 'penguin' in i.get('title', '').lower():
                    print "PENGUIN" * 80
                    print i
                print i.get('title', '[no title]')
                yield i

    def metadata_lookup(self, identifier):
        """Look up metadata for an Overdrive identifier.
        """
        url = self.METADATA_ENDPOINT % dict(
            collection_token=self.collection_token,
            item_id=identifier.identifier
        )
        representation, cached = Representation.get(
            self._db, url, self.get, data_source=self.source,
            identifier=identifier)
        return json.loads(representation.content)

    def update_licensepool(self, book):
        """Update availability information for a single book.

        If the book has never been seen before, a new LicensePool
        will be created for the book.

        The book's LicensePool will be updated with current
        circulation information.
        """
        # Retrieve current circulation information about this book
        orig_book = book
        if isinstance(book, basestring):
            book_id = book
            circulation_link = self.AVAILABILITY_ENDPOINT % dict(
                collection_name=self.collection_name,
                product_id=book_id
            )
            book = dict(id=book_id)
        else:
            circulation_link = book['availability_link']
        status_code, headers, content = self.get(circulation_link, {})
        if status_code != 200:
            print "ERROR: Could not get availability for %s: %s" % (
                book['id'], status_code)
            return None, None

        book.update(json.loads(content))
        return self.update_licensepool_with_book_info(book)

    def update_licensepool_with_book_info(self, book):
        """Update a book's LicensePool with information from a JSON
        representation of its circulation info.

        Also adds very basic bibliographic information to the Edition.
        """
        overdrive_id = book['id']
        pool, was_new = LicensePool.for_foreign_id(
            self._db, self.source, Identifier.OVERDRIVE_ID, overdrive_id)
        if was_new:
            pool.open_access = False
            wr, wr_new = Edition.for_foreign_id(
                self._db, self.source, Identifier.OVERDRIVE_ID, overdrive_id)
            if 'title' in book:
                wr.title = book['title']
            print "New book: %r" % wr

        new_licenses_owned = []
        new_licenses_available = []
        new_number_of_holds = []
        if 'collections' in book:
            for collection in book['collections']:
                if 'copiesOwned' in collection:
                    new_licenses_owned.append(collection['copiesOwned'])
                if 'copiesAvailable' in collection:
                    new_licenses_available.append(collection['copiesAvailable'])
                if 'numberOfHolds' in collection:
                    new_number_of_holds.append(collection['numberOfHolds'])

        if new_licenses_owned:
            new_licenses_owned = sum(new_licenses_owned)
        else:
            new_licenses_owned = pool.licenses_owned

        if new_licenses_available:
            new_licenses_available = sum(new_licenses_available)
        else:
            new_licenses_available = pool.licenses_available

        if new_number_of_holds:
            new_number_of_holds = sum(new_number_of_holds)
        else:
            new_number_of_holds = pool.patrons_in_hold_queue

        # Overdrive doesn't do 'reserved'.
        licenses_reserved = 0

        print " Owned: %s => %s" % (pool.licenses_owned, new_licenses_owned)
        print " Available: %s => %s" % (pool.licenses_available, new_licenses_available)
        print " Holds: %s => %s" % (pool.patrons_in_hold_queue, new_number_of_holds)

        pool.update_availability(new_licenses_owned, new_licenses_available,
                                 licenses_reserved, new_number_of_holds)
        return pool, was_new

    def _get_book_list_page(self, link):
        """Process a page of inventory whose circulation we need to check.

        Returns a list of (title, id, availability_link) 3-tuples,
        plus a link to the next page of results.
        """
        # We don't cache this because it changes constantly.
        status_code, headers, content = self.get(link, {})
        try:
            data = json.loads(content)
        except Exception, e:
            print "ERROR: %r %r %r" % (status_code, headers, content)
            return [], None

        # Find the link to the next page of results, if any.
        next_link = OverdriveRepresentationExtractor.link(data, 'next')

        # Prepare to get availability information for all the books on
        # this page.
        availability_queue = (
            OverdriveRepresentationExtractor.availability_link_list(data))
        return availability_queue, next_link

    @classmethod
    def get_download_link(self, checkout_response, format_type, error_url):
        link = None
        format = None
        for f in checkout_response['formats']:
            if f['formatType'] == format_type:
                format = f
                break
        if not format:
            raise IOError("Could not find specified format %s" % format_type)

        if not 'linkTemplates' in format:
            raise IOError("No linkTemplates for format %s" % format_type)
        templates = format['linkTemplates']
        if not 'downloadLink' in templates:
            raise IOError("No downloadLink for format %s" % format_type)
        download_link = templates['downloadLink']['href']
        if download_link:
            return download_link.replace("{errorpageurl}", error_url)
        else:
            return None

    @classmethod
    def make_link_safe(self, url):
        """Turn a server-provided link into a link the server will accept!

        This is completely obnoxious and I have complained about it to
        Overdrive.
        """
        parts = list(urlparse.urlsplit(url))
        parts[2] = urllib.quote(parts[2])
        query_string = parts[3]
        query_string = query_string.replace("+", "%2B")
        query_string = query_string.replace(":", "%3A")
        query_string = query_string.replace("{", "%7B")
        query_string = query_string.replace("}", "%7D")
        parts[3] = query_string
        return urlparse.urlunsplit(tuple(parts))
            

class OverdriveRepresentationExtractor(object):

    """Extract useful information from Overdrive's JSON representations."""

    @classmethod
    def availability_link_list(self, book_list):
        """:return: A list of dictionaries with keys `id`, `title`,
        `availability_link`.
        """
        l = []
        if not 'products' in book_list:
            return []

        products = book_list['products']
        for product in products:
            data = dict(id=product['id'],
                        title=product['title'],
                        author_name=None)
            
            if 'primaryCreator' in product:
                creator = product['primaryCreator']
                if creator.get('role') == 'Author':
                    data['author_name'] = creator.get('name')
            links = product.get('links', [])
            if 'availability' in links:
                link = links['availability']['href']
                data['availability_link'] = OverdriveAPI.make_link_safe(link)
            else:
                log.warn("No availability link for %s" % book_id)
            l.append(data)
        return l

    @classmethod
    def link(self, page, rel):
        if 'links' in page and rel in page['links']:
            raw_link = page['links'][rel]['href']
            link = OverdriveAPI.make_link_safe(raw_link)
        else:
            link = None
        return link



class OverdriveCirculationMonitor(Monitor):
    """Maintain license pool for Overdrive titles.

    This is where new books are given their LicensePools.  But the
    bibliographic data isn't inserted into those LicensePools until
    the OverdriveCoverageProvider runs.
    """
    def __init__(self, _db):
        super(OverdriveCirculationMonitor, self).__init__(
            "Overdrive Circulation Monitor")
        self._db = _db
        self.api = OverdriveAPI(self._db)

    def recently_changed_ids(self, start, cutoff):
        return self.api.recently_changed_ids(start, cutoff)

    def run_once(self, _db, start, cutoff):
        added_books = 0
        overdrive_data_source = DataSource.lookup(
            _db, DataSource.OVERDRIVE)

        i = None
        for i, book in enumerate(self.recently_changed_ids(start, cutoff)):
            if i > 0 and not i % 50:
                print " %s processed" % i
            if not book:
                continue
            license_pool, is_new = self.api.update_licensepool(book)
            # Log a circulation event for this work.
            if is_new:
                CirculationEvent.log(
                    _db, license_pool, CirculationEvent.TITLE_ADD,
                    None, None, start=license_pool.last_checked)
            _db.commit()
        if i != None:
            print "Processed %d books total." % (i+1)

class OverdriveBibliographicMonitor(CoverageProvider):
    """Fill in bibliographic metadata for Overdrive records."""

    def __init__(self, _db):
        self._db = _db
        self.overdrive = OverdriveAPI(self._db)
        self.input_source = DataSource.lookup(_db, DataSource.OVERDRIVE)
        self.output_source = DataSource.lookup(_db, DataSource.OVERDRIVE)
        super(OverdriveBibliographicMonitor, self).__init__(
            "Overdrive Bibliographic Monitor",
            self.input_source, self.output_source)

    @classmethod
    def _add_value_as_resource(cls, input_source, identifier, pool, rel, value,
                               media_type="text/plain", url=None):
        if isinstance(value, str):
            value = value.decode("utf8")
        elif isinstance(value, unicode):
            pass
        else:
            value = str(value)
        identifier.add_resource(
            rel, url, input_source, pool, media_type, value)

    @classmethod
    def _add_value_as_measurement(
            cls, input_source, identifier, quantity_measured, value):
        if isinstance(value, str):
            value = value.decode("utf8")
        elif isinstance(value, unicode):
            pass

        value = float(value)
        identifier.add_measurement(
            input_source, quantity_measured, value)

    DATE_FORMAT = "%Y-%m-%d"

    def process_edition(self, wr):
        identifier = wr.primary_identifier
        info = self.overdrive.metadata_lookup(identifier)
        return self.annotate_edition_with_bibliographic_information(
            self._db, wr, info, self.input_source
        )

    media_type_for_overdrive_type = {
        "ebook-pdf-adobe" : "application/pdf",
        "ebook-pdf-open" : "application/pdf",
        "ebook-epub-adobe" : "application/epub+zip",
        "ebook-epub-open" : "application/epub+zip",
    }
        
    @classmethod
    def annotate_edition_with_bibliographic_information(
            cls, _db, wr, info, input_source):

        identifier = wr.primary_identifier
        license_pool = wr.license_pool

        # First get the easy stuff.
        wr.title = info['title']
        wr.subtitle = info.get('subtitle', None)
        wr.series = info.get('series', None)
        wr.publisher = info.get('publisher', None)
        wr.imprint = info.get('imprint', None)

        if 'publishDate' in info:
            wr.published = datetime.datetime.strptime(
                info['publishDate'][:10], cls.DATE_FORMAT)

        languages = [
            LanguageCodes.two_to_three.get(l['code'], l['code'])
            for l in info.get('languages', [])
        ]
        if 'eng' in languages or not languages:
            wr.language = 'eng'
        else:
            wr.language = sorted(languages)[0]

        # TODO: Is there a Gutenberg book with this title and the same
        # author names? If so, they're the same. Merge the work and
        # reuse the Contributor objects.
        #
        # Or, later might be the time to do that stuff.

        for creator in info.get('creators', []):
            name = creator['fileAs']
            display_name = creator['name']
            role = creator['role']
            contributor = wr.add_contributor(name, role)
            contributor.display_name = display_name
            if 'bioText' in creator:
                contributor.extra = dict(description=creator['bioText'])

        for i in info.get('subjects', []):
            c = identifier.classify(input_source, Subject.OVERDRIVE, i['value'])

        wr.sort_title = info.get('sortTitle')
        extra = dict()
        for inkey, outkey in (
                ('gradeLevels', 'grade_levels'),
                ('mediaType', 'medium'),
                ('awards', 'awards'),
        ):
            if inkey in info:
                extra[outkey] = info.get(inkey)
        wr.extra = extra

        # Associate the Overdrive Edition with other identifiers
        # such as ISBN.
        for format in info.get('formats', []):
            for new_id in format.get('identifiers', []):
                t = new_id['type']
                v = new_id['value']
                type_key = None
                if t == 'ASIN':
                    type_key = Identifier.ASIN
                elif t == 'ISBN':
                    type_key = Identifier.ISBN
                    if len(v) == 10:
                        v = isbnlib.to_isbn13(v)
                elif t == 'DOI':
                    type_key = Identifier.DOI
                elif t == 'UPC':
                    type_key = Identifier.UPC
                elif t == 'PublisherCatalogNumber':
                    continue
                if type_key:
                    new_identifier, ignore = Identifier.for_foreign_id(
                        _db, type_key, v)
                    identifier.equivalent_to(
                        input_source, new_identifier, 1)

            # Samples become resources.
            if 'samples' in format:
                if format['id'] == 'ebook-overdrive':
                    # Useless to us.
                    continue
                media_type = cls.media_type_for_overdrive_type.get(
                    format['id'])
                if not media_type:
                    print format['id']
                    set_trace()
                for sample_info in format['samples']:
                    href = sample_info['url']
                    resource, new = identifier.add_resource(
                        Resource.SAMPLE, href, input_source,
                        license_pool, media_type)
                    resource.file_size = format['fileSize']

        # Add resources: cover and descriptions

        if 'images' in info and 'cover' in info['images']:
            link = info['images']['cover']
            href = OverdriveAPI.make_link_safe(link['href'])
            media_type = link['type']
            identifier.add_resource(Resource.IMAGE, href, input_source,
                                    license_pool, media_type)

        short = info.get('shortDescription')
        full = info.get('fullDescription')

        if full:
            cls._add_value_as_resource(
                input_source, identifier, license_pool, Resource.DESCRIPTION, full,
                "text/html", "tag:full")

        if short and short != full and (not full or not full.startswith(short)):
            cls._add_value_as_resource(
                input_source, identifier, license_pool, Resource.DESCRIPTION, short,
                "text/html", "tag:short")

        # Add measurements: rating and popularity
        if info.get('starRating') is not None and info['starRating'] > 0:
            cls._add_value_as_measurement(
                input_source, identifier, Measurement.RATING,
                info['starRating'])

        if info['popularity']:
            cls._add_value_as_measurement(
                input_source, identifier, Measurement.POPULARITY,
                info['popularity'])

        return True

class OverdriveCoverImageMirror(CoverImageMirror):
    """Downloads images from Overdrive and writes them to disk."""

    ORIGINAL_PATH_VARIABLE = "original_overdrive_covers_mirror"
    SCALED_PATH_VARIABLE = "scaled_overdrive_covers_mirror"
    DATA_SOURCE = DataSource.OVERDRIVE
