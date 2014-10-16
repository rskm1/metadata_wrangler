import os
from nose.tools import set_trace
from lxml import etree
import re

from collections import Counter, defaultdict

from model import (
    Contributor,
    DataSource,
    Representation,
)

from integration import (
    XMLParser,
)

class VIAFParser(XMLParser):

    NAMESPACES = {'ns2' : "http://viaf.org/viaf/terms#"}

    def info(self, contributor, xml):
        """For the given Contributor, find:

        * A display name (that can go on a book cover)
        * A family name (to have a short way of referring to the author)
        * A Wikipedia name (so we can get access to Wikipedia,
          Wikidata, WikiQuotes, etc.)

        :return: a 3-tuple (display name, family name, Wikipedia name)
        """
        if not xml:
            display, family = contributor.default_names(None)
            return display, family, None
        print "Starting point: %s" % contributor.name
        display_name, family_name, wikipedia_name = self.parse(
            xml, contributor.name)
        
        if not display_name or not family_name:
            default_family, default_display = contributor.default_names(None)
            if not display_name:
                display_name = default_display
            if not family_name:
                family_name = default_family

        if wikipedia_name:
            print " Wikipedia name: %s" % wikipedia_name
        print " Display name: %s" % display_name
        print " Family name: %s" % family_name
        print
        return display_name, family_name, wikipedia_name

    def parse(self, xml, working_name=None):
        """Parse a VIAF response into a name 3-tuple."""
        tree = etree.fromstring(xml, parser=etree.XMLParser(recover=True))
        display_name = None
        family_name = None
        wikipedia_name = None
        # Does this author have a Wikipedia page?
        for source in self._xpath(tree, "ns2:sources/ns2:source"):
            if source.text.startswith("WKP|"):
                # Jackpot!
                wikipedia_name = source.text[4:]
                display_name = wikipedia_name.replace("_", " ")
                if ' (' in display_name:
                    display_name = display_name[:display_name.rindex(' (')]
                # We're so happy about this we're going to overwrite
                # any incoming display name.
                working_name = display_name

        # If we found a Wikipedia name, we still need to find a family name.
        # If we didn't find a Wikipedia name, we need to find both a family
        # name and a display name. We do this by going through UNIMARC
        # records.
        unimarcs = self._xpath(tree, '//ns2:datafield[@dtype="UNIMARC"]')
        candidates = []
        for unimarc in unimarcs:
            (possible_given, possible_family,
             possible_extra) = self.extract_name_from_unimarc(unimarc)
            # Some part of this name must also show up in the original
            # name for it to even be considered. Otherwise it's a
            # better bet to try to munge the original name.
            for v in (possible_given, possible_family, possible_extra):
                if (not working_name) or (v and v in working_name):
                    candidates.append((possible_given, possible_family,
                                       possible_extra))
                    break
            else:
                #print "  EXCLUDED %s/%s/%s for lack of resemblance to %s" % (
                #    possible_given, possible_family, possible_extra,
                #    working_name)
                pass

        display_nameparts = self.best_choice(candidates)
        if display_nameparts[1]: # Family name
            family_name = display_nameparts[1]
        return (
            display_name or self.combine_nameparts(*display_nameparts),
            family_name,
            wikipedia_name)

    def best_choice(self, possibilities):
        """Return the best (~most popular) choice among the given names.

        :param possibilities: A list of (given, family, extra) 3-tuples.
        """
        if not possibilities:
            return None, None, None
        elif len(possibilities) == 1:
            # There is only one choice. Use it.
            return possibilities[0]

        # There's more than one choice, so it's gonna get
        # complicated. First, find the most common family name.
        family_names = Counter()
        given_name_for_family_name = defaultdict(Counter)
        extra_for_given_name_and_family_name = defaultdict(Counter)
        for given_name, family_name, name_extra in possibilities:
            print "  POSSIBILITY: %s/%s/%s" % (
                given_name, family_name, name_extra)
            if family_name:
                family_names[family_name] += 1
                if given_name:
                    given_name_for_family_name[family_name][given_name] += 1
                    extra_for_given_name_and_family_name[(family_name, given_name)][name_extra] += 1
        if not family_names:
            # None of these are useful.
            return None, None, None
        family_name = family_names.most_common(1)[0][0]

        given_name = None
        name_extra = None

        # Now find the most common given name, given the most
        # common family name.
        given_names = given_name_for_family_name[family_name]
        if given_names:
            given_name = given_names.most_common(1)[0][0]
            extra = extra_for_given_name_and_family_name[
                (family_name, given_name)]
            if extra:
                name_extra, count = extra.most_common(1)[0]

                # Don't add extra stuff on to the name if it's a
                # viable option.
                if extra[None] == count:
                    name_extra = None
        return given_name, family_name, name_extra

    def remove_commas_from(self, namepart):
        """Strip dangling commas from a namepart."""
        if namepart.endswith(","):
            namepart = namepart[:-1]
        if namepart.startswith(","):
            namepart = namepart[1:]
        return namepart.strip()

    def extract_name_from_unimarc(self, unimarc):
        """Turn a UNIMARC tag into a 3-tuple:
         (given name, family name, extra)
        """
        data = dict()
        for (code, key) in (
                ('a', 'family'),
                ('b', 'given'),
                ('c', 'extra'),
                ):
            value = self._xpath1(unimarc, 'ns2:subfield[@code="%s"]' % code)
            if value is not None and value.text:
                value = value.text
                value = self.remove_commas_from(value)
                data[key] = value
        return (data.get('given', None), data.get('family', None),
                data.get('extra', None))

    @classmethod
    def combine_nameparts(self, given, family, extra):
        """Turn a (given name, family name, extra) 3-tuple into a
        display name.
        """
        if not given and not family:
            return None
        if family and not given:
            display_name = family
        elif given and not family:
            display_name = given
        else:
            display_name = given + ' ' + family
        if extra and not extra.startswith('pseud'):
            if family and given:
                display_name += ', ' + extra
            else:
                display_name += ' ' + extra
        return display_name


class VIAFClient(object):

    BASE_URL = 'http://viaf.org/viaf/%(viaf)s/viaf.xml'
    SUBDIR = "viaf"

    def __init__(self, _db):
        self._db = _db
        self.data_source = DataSource.lookup(self._db, DataSource.VIAF)
        self.parser = VIAFParser()

    def run(self, force=False):
        a = 0
        candidates = self._db.query(Contributor)
        if not force:
            # Only process authors that haven't been processed yet.
            candidates = candidates.filter(Contributor.display_name==None)
        for contributor in candidates:
            # Sometimes 'viaf' is multiple values.
            if contributor.viaf:
                viafs = [x.strip() for x in contributor.viaf.split("|")]
                for v in viafs:
                    url = self.BASE_URL % dict(viaf=v)
                    r, cached = Representation.get(self._db, url, data_source=self.data_source)

                    xml = r.content
                    display_name, family_name, wikipedia_name = self.parser.info(
                        contributor, xml)
                    contributor.display_name = display_name
                    contributor.family_name = family_name
                    contributor.wikipedia_name = wikipedia_name
                    if wikipedia_name or family_name:
                        # Good enough.
                        break
            if not contributor.display_name:
                # We could not find a VIAF record for this contributor,
                # or none of the records were good enough. Use the
                # default name.
                contributor.family_name, contributor.display_name = (
                    contributor.default_names())
            a += 1
            if not a % 1000:
                print a
                self._db.commit()
        self._db.commit()
