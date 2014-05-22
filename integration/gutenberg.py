import datetime
import os
import json
import random
import re
import requests
import time
import shutil
import tarfile
from StringIO import StringIO

from nose.tools import set_trace

import rdflib
from rdflib import Namespace

from model import (
    get_one_or_create,
    WorkRecord,
    DataSource,
    WorkIdentifier,
    LicensePool,
    SubjectType,
)

from monitor import Monitor
#from integration.oclc import (
#    OCLC,
#    OCLCClassifyAPI,
#)

class GutenbergAPI(object):

    """An 'API' to Project Gutenberg's RDF catalog.

    A bit different from the other APIs since the data comes over the
    web all at once in one big BZ2 file.
    """

    ID_IN_FILENAME = re.compile("pg([0-9]+).rdf")

    EVENT_SOURCE = "Gutenberg"
    FILENAME = "rdf-files.tar.bz2"

    ONE_DAY = 60 * 60 * 24

    MIRRORS = [
        "http://www.gutenberg.org/cache/epub/feeds/rdf-files.tar.bz2",
        "http://gutenberg.readingroo.ms/cache/generated/feeds/rdf-files.tar.bz2",
        "http://snowy.arsc.alaska.edu/gutenberg/cache/generated/feeds/rdf-files.tar.bz2",        
    ] 

    def __init__(self, data_directory):
        self.data_directory = data_directory
        self.catalog_path = os.path.join(self.data_directory, self.FILENAME)

    def update_catalog(self):
        """Download the most recent Project Gutenberg catalog
        from a randomly selected mirror."""
        url = random.choice(self.MIRRORS)
        print "Refreshing %s" % url
        data = requests.get(url)
        tmp_path = self.catalog_path + ".tmp"
        open(tmp_path, "wb").write(data.content)
        shutil.move(tmp_path, self.catalog_path)

    def needs_refresh(self):
        """Is it time to download a new version of the catalog?"""
        if os.path.exists(self.catalog_path):
            modification_time = os.stat(self.catalog_path).st_mtime
            return (time.time() - modification_time) >= self.ONE_DAY
        return True

    def all_books(self):
        """Yields raw data for every book in the PG catalog."""
        if self.needs_refresh():
            self.update_catalog()
        archive = tarfile.open(self.catalog_path)
        next_item = archive.next()
        a = 0
        while next_item:
            if next_item.isfile() and next_item.name.endswith(".rdf"):
                pg_id = self.ID_IN_FILENAME.search(next_item.name).groups()[0]
                yield pg_id, archive, next_item
            next_item = archive.next()

    def create_missing_books(self, _db):
        """Finds books present in the PG catalog but missing from WorkRecord.

        Yields (WorkRecord, LicensePool) 2-tuples.
        """
        books = self.all_books()
        source = DataSource.GUTENBERG
        for pg_id, archive, archive_item in books:
            print "Considering %s" % pg_id

            # Find an existing WorkRecord for the book.
            book = _db.query(WorkRecord).filter_by(
                source=source, source_id=pg_id,
                source_id_type=WorkIdentifier.GUTENBERG
            ).first()

            if book is None:
                # Create a new WorkRecord object with bibliographic
                # information from the Project Gutenberg RDF file.
                print "%s is new." % pg_id
                fh = archive.extractfile(archive_item)
                data = fh.read()
                fake_fh = StringIO(data)
                book = GutenbergRDFExtractor.book_in(_db, pg_id, fake_fh)

            # Ensure that an open-access LicensePool exists for this book.
            license = get_one_or_create(
                _db, LicensePool,
                data_source=book.data_source,
                identifier=book.primary_identifier,
                create_method_kwargs=dict(
                    open_access=True,
                    last_checked=datetime.datetime.now(),
                )
            )
            yield (book, license)
                    
 
class GutenbergRDFExtractor(object):

    """Transform a Project Gutenberg RDF description of a title into a
    WorkRecord object and an open-access LicensePool object.
    """

    dcterms = Namespace("http://purl.org/dc/terms/")
    dcam = Namespace("http://purl.org/dc/dcam/")
    rdf = Namespace(u'http://www.w3.org/1999/02/22-rdf-syntax-ns#')
    gutenberg = Namespace("http://www.gutenberg.org/2009/pgterms/")

    ID_IN_URI = re.compile("/([0-9]+)$")

    FORMAT = "format"

    DATE_FORMAT = "%Y-%m-%d"

    @classmethod
    def _values(cls, graph, query):
        """Return just the values of subject-predicate-value triples."""
        return [x[2] for x in graph.triples(query)]

    @classmethod
    def _value(cls, graph, query):
        """Return just one value for a subject-predicate-value triple."""
        v = cls._values(graph, query)
        if v:
            return v[0]
        return None

    @classmethod
    def book_in(cls, _db, pg_id, fh):
        """Yield a WorkRecord object for the book described by the given
        filehandle, creating it (but not committing it) if necessary.

        This assumes that there is at most one book per
        filehandle--the one identified by ``pg_id``. However, a file
        may turn out to describe no books at all (such as pg_id=1984,
        reserved for George Orwell's "1984"). In that case,
        ``book_in()`` will return None.
        """
        g = rdflib.Graph()
        g.load(fh)
        data = dict()

        # Determine the 'about' URI.
        title_triples = list(g.triples((None, cls.dcterms['title'], None)))

        book = None
        if title_triples:
            if len(title_triples) > 1:
                # Each filehandle is associated with one Project Gutenberg ID 
                # and should thus contain at most one title.
                raise ValueError(
                    "More than one title associated with Project Gutenberg ID %s" % pg_id)
            uri, ignore, title = title_triples[0]
            book = cls.parse_book(_db, g, uri, title)
        return book

    @classmethod
    def parse_book(cls, _db, g, uri, title):
        """Turn an RDF graph into a WorkRecord for the given `uri` and
        `title`.
        """
        source_id = unicode(cls.ID_IN_URI.search(uri).groups()[0])
        # Split a subtitle out from the main title.
        title = unicode(title)
        subtitle = None
        for separator in "\r\n", "\n":
            if separator in title:
                parts = title.split(separator)
                title = parts[0]
                subtitle = "\n".join(parts[1:])
                break
        print " %s" % title

        issued = cls._value(g, (uri, cls.dcterms.issued, None))
        issued = datetime.datetime.strptime(issued, cls.DATE_FORMAT).date()

        summary = cls._value(g, (uri, cls.dcterms.description, None))
        summary = WorkRecord._content(summary)
        
        publisher = cls._value(g, (uri, cls.dcterms.publisher, None))

        languages = []
        for ignore, ignore, language_uri in g.triples(
                (uri, cls.dcterms.language, None)):
            code = str(cls._value(g, (language_uri, cls.rdf.value, None)))
            languages.append(code)

        links = [WorkRecord._link("canonical", uri)]
        download_links = cls._values(g, (uri, cls.dcterms.hasFormat, None))
        for link in download_links:
            for format_uri in cls._values(
                    g, (link, cls.dcterms['format'], None)):
                media_type = cls._value(g, (format_uri, cls.rdf.value, None))
                link = WorkRecord._link(
                    WorkRecord.OPEN_ACCESS_DOWNLOAD, link, media_type)
                links.append(link)
        
        subjects = []
        subject_links = cls._values(g, (uri, cls.dcterms.subject, None))
        for subject in subject_links:
            value = cls._value(g, (subject, cls.rdf.value, None))
            vocabulary = cls._value(g, (subject, cls.dcam.memberOf, None))
            vocabulary=SubjectType.by_uri[str(vocabulary)]
            subjects.append(WorkRecord._subject(vocabulary, value))

        authors = []
        for ignore, ignore, author_uri in g.triples((uri, cls.dcterms.creator, None)):
            name = cls._value(g, (author_uri, cls.gutenberg.name, None))
            aliases = cls._values(g, (author_uri, cls.gutenberg.alias, None))
            authors.append(WorkRecord._author(name, aliases=aliases))

        # Create or fetch a WorkRecord for this book.
        source = DataSource.lookup(_db, DataSource.GUTENBERG)
        identifier, new = WorkIdentifier.for_foreign_id(
            _db, WorkIdentifier.GUTENBERG_ID, source_id)
        book, new = get_one_or_create(
            _db, WorkRecord,
            create_method_kwargs=dict(
                title=title,
                subtitle=subtitle,
                issued=issued,
                summary=summary,
                publisher=publisher,
                languages=languages,
                links=links,
                subjects=subjects,
                authors=authors),
            data_source=source,
            primary_identifier=identifier,
        )

        return book, new


class GutenbergMonitor(object):
    """Maintain license pool and metadata info for Gutenberg titles.
    """

    def __init__(self, data_directory):
        path = os.path.join(data_directory, WorkRecordSource.GUTENBERG)
        if not os.path.exists(path):
            os.makedirs(path)
        self.source = GutenbergAPI(path)
        self.circulation_events = FilesystemMonitorStore(path)

    def run(self):
        added_books = 0
        for work, license_pool in self.source.missing_books():
            event = CirculationEvent._get_one_or_add(
                type=CirculationEvent.TITLE_ADD,
                license_pool=license_pool,
                create_method_kwargs=dict(
                    start=license_pool.last_checked
                )
            )
            _db.commit()
            #event.log()


class OCLCMonitorForGutenberg(object):

    """Track OCLC's opinions about books with the same title/author as 
    Gutenberg works."""

    # Strips most non-alphanumerics from the title.
    # 'Alphanumerics' includes alphanumeric characters
    # for any language, so this shouldn't affect
    # titles in non-Latin languages.
    #
    # OCLC has trouble recognizing non-alphanumerics in titles,
    # especially colons.
    NON_TITLE_SAFE = re.compile("[^\w\-' ]", re.UNICODE)
    
    def __init__(self, data_directory):
        self.gutenberg = GutenbergMonitor(data_directory)
        self.oclc = OCLCClassifyAPI(data_directory)
        self.cache = self.oclc.cache
        self.processed = FilesystemStore(
            self.oclc.cache_directory, 'gutenberg.works.last_update', 'gutenberg.works', "Gutenberg.ID")

    def oclc_safe_title(self, title):
        return self.NON_TITLE_SAFE.sub("", title)

    def title_and_author(self, book):
        title = self.oclc_safe_title(book.title)

        authors = book.authors
        if len(authors) == 0:
            author = ''
        else:
            author = authors[0]['name']
        return title, author

    def run(self, _db):
        i = 0
        # Look up all the WorkRecords we acquired directly
        # from Project Gutenberg.
        counter = 0
        for book in _db.query(WorkRecord).filter_by(
                source=WorkRecordSource.GUTENBERG,
                source_id_type=WorkIdentifier.GUTENBERG):
            title, author = self.title_and_author(book)

            # For each such record, check whether we have a WorkRecord
            # from OCLC for the book's title and author. Do *not*
            # create this record if it doesn't exist; that's the job
            # of records_for().
            source_id = self.oclc.query_string(title=title, author=author)
            workset_record = _db.query(WorkRecord).filter_by(
                source=WorkRecordSource.OCLC, 
                source_id=source_id,
                source_id_type=WorkIdentifier.QUERY_STRING
            ).first()
            if workset_record:
                # We already did this one.
                print 'IGNORE %s "%s" "%s"' % (source_id, title, author)
                continue

            print '%s "%s" "%s"' % (source_id, title, author)
            # Perform the title/author lookup
            xml = self.oclc.lookup_by(title=title, author=author)

            # Turn the raw XML into some number of bibliographic records.
            workset_record, work_records, edition_records = (
                self.oclc.records_for(source_id, xml))

            if work_records:
                print " Created %s work records(s)." % len(work_records)
            if edition_records:
                print " Created %s edition records(s)." % len(edition_records)

            if work_records and not edition_records:
                # Our search turned up a number of works, but no
                # editions. Editions are what we're really after--they
                # tend to have juicy ISBNs. Look up some editions.
                print " Looking up the top 5 of %s works." % len(work_records)
                print "-" * 80
                works_looked_up = 0
                for work_record in work_records:
                    if work_record.source_id_type == WorkIdentifier.OCLC_SWID:
                        swid = work_record.source_id
                        raw = self.oclc.lookup_by(swid=swid)
                        query_string = "swid=%s" % swid
                        ignore, ignore, editions = self.oclc.records_for(
                            query_string, raw)
                        edition_records.extend(editions)
                    works_looked_up += 1
                    if works_looked_up >= 5:
                        break
                print "-" * 80
                print " Now there are %s edition record(s)." % len(edition_records)
            counter += 1
            if not counter % 10:
                _db.commit()
        _db.commit()
