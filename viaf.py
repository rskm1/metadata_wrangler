import logging
import os
import re

from nose.tools import set_trace
from lxml import etree
from fuzzywuzzy import fuzz

from collections import Counter, defaultdict

from core.metadata_layer import (
    ContributorData,
    Metadata, 
)

from core.model import (
    Contributor,
    DataSource,
    Representation,
)

from core.util.personal_names import (
    contributor_name_match_ratio, 
    display_name_to_sort_name, 
    is_corporate_name, 
    normalize_contributor_name_for_matching, 
)

from core.util.titles import (
    normalize_title_for_matching, 
    title_match_ratio, 
    unfluff_title, 
)

from core.util.xmlparser import (
    XMLParser,
)


class VIAFParser(XMLParser):

    NAMESPACES = {'ns2' : "http://viaf.org/viaf/terms#"}

    log = logging.getLogger("VIAF Parser")
    wikidata_id = re.compile("^Q[0-9]")

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


    @classmethod
    def name_matches(cls, n1, n2):
        """ Returns true if n1 and n2 are identical strings, bar periods.  
        """ 
        return n1.replace(".", "").lower() == n2.replace(".", "").lower()


    @classmethod
    def prepare_contributor_name_for_matching(cls, name):
        """
        Normalize the special characters and inappropriate spacings away.
        Put the name into title, first, middle, last, suffix, nickname order, 
        and lowercase.
        """
        return normalize_contributor_name_for_matching(name)


    @classmethod
    def weigh_contributor(cls, candidate, working_sort_name, known_titles=None, strict=False, ignore_popularity=False):
        """ Find the author who corresponds the best to the working_sort_name.
            Consider as evidence of suitability: 
            - top-most in viaf-returned xml (most popular in libraries) 
            - various name/pseudonym fields within xml match 
            - has written titles that match ones passed in.

            Actual weight numbers do not matter, only their weights relative to each other.
            So, if the total match confidence is 110%, that's acceptable, and may not even 
            be the best match if there's a 120% out there.  But having an exact title match 
            does matter more than a fuzzy unimarc tag match.  
        """
        report_string = "no_viaf"
        (contributor, match_confidences, contributor_titles) = candidate

        if contributor.viaf:
            report_string = "viaf=%s" % contributor.viaf

        if not match_confidences:
            # we didn't get it from the xml, but we'll add to it now
            match_confidences = {}

        # If we're not sure that this is even the right cluster for
        # the given author, make sure that one of the working names
        # shows up in a name record.
        if strict:
            if not match_confidences:
                return 0

        # Assign weights to fields matched in the xml.  
        # The fuzzy matching returned a number between 0 and 100, 
        # now tell the system that we find sort_name to be a more reliable indicator 
        # than unimarc flags.  
        # Weights are cumulative -- if both the sort and display name match, that helps us 
        # be extra special sure.  But what to do if unimarc tags match and sort_name doesn't? 
        # Here's where the strict tag comes in.  With strict, a failed sort_name match says "no" 
        # to any other suggestions of a possible fit.
        match_confidences["total"] = 0

        if "library_popularity" in match_confidences and not ignore_popularity:
            match_confidences["total"] += -10 * match_confidences["library_popularity"]
            report_string += ", pop=10 * %s" % match_confidences["library_popularity"]

        if "sort_name" in match_confidences:
            # fuzzy match filter may not always give a 100% match, so cap arbitrarily at 90% as a "sure match"
            if strict and match_confidences["sort_name"] < 90:
                match_confidences["total"] = 0
                report_string += ", strict and no sort_name match, return 0 (%s)" % match_confidences["sort_name"]
                return 0

            match_confidences["total"] += 2 * match_confidences["sort_name"]
            report_string += ", mc[sort_name]= %s" % match_confidences["sort_name"]

        if "display_name" in match_confidences:
            match_confidences["total"] += 0.5 * match_confidences["display_name"]
            report_string += ", mc[display_name]=%s" % match_confidences["display_name"]

        if "unimarc" in match_confidences:
            match_confidences["total"] += 0.3 * match_confidences["unimarc"]
            report_string += ", mc[unimarc]=%s" % match_confidences["unimarc"]

        if "guessed_sort_name" in match_confidences:
            match_confidences["total"] += 0.5 * match_confidences["guessed_sort_name"]
            report_string += ", mc[guessed_sort_name]=%s" % match_confidences["guessed_sort_name"]

        if "alternate_name" in match_confidences:
            match_confidences["total"] += 0.2 * match_confidences["alternate_name"]
            report_string += ", mc[alternate_name]=%s" % match_confidences["alternate_name"]

        # Add in some data quality evidence.  We want the contributor to have recognizable 
        # data to work with.
        if contributor.display_name:
            match_confidences["total"] += 0.2
            report_string += ", have contributor.display_name=%s" % contributor.display_name

        if contributor.viaf:
            match_confidences["total"] += 0.2

        cls.weigh_titles(known_titles, contributor_titles, match_confidences, strict)
        if "title" in match_confidences:
            report_string += ", mc[title]=%s" % match_confidences["title"]

        report_string += ", mc[total]= %s" % match_confidences["total"]
        cls.log.debug("weigh_contributor found: " + report_string)

        # TODO:  in the calling code, create a cloud of interrelated contributors
        # around the primary picked on, with relevancy weights given by this.
        return match_confidences["total"]


    @classmethod
    def weigh_titles(cls, known_titles=None, contributor_titles=None, match_confidences=None, strict=False):
        if known_titles:
            for known_title in known_titles:
                if strict: 
                    if known_title in contributor_titles:
                        match_confidences["title"] = 100
                        match_confidences["total"] += 0.8 * match_confidences["title"]
                        # once we find one matching title, no need to keep looking
                        break
                else:
                    for contributor_title in contributor_titles:
                        # when the second half of the title has something like:
                        # "Edited by", a colon or semicolon, a bracket or parentheses, a hyphen, 
                        # one of the institutional authors, like Disney Book Group, elibrary, Inc, 
                        # Harvard University, Harper & Brothers, 
                        # then see if can get an exact substring match on the title.
                        # We want to accept "Pride and Prejudice (Unabridged)" as equivalent to 
                        # "Pride and Prejudice", but reject "Pride and Prejudice and Zombies" 
                        # as probably not written by Jane Austen. 
                        # TODO: In future, consider doing:
                        # "Pride and Prejudice (Spanish)" should connect to two authors -- 
                        # Jane Austen and the translator.
                        if cls.name_matches(unfluff_title(contributor_title), unfluff_title(known_title)):
                            match_confidences["title"] = 90
                            match_confidences["total"] += 0.8 * match_confidences["title"]
                            # match is good enough, we can stop
                            break

                        '''
                        Fixes issue where 
                        <ns1:title>Britain, detente and changing east-west relations</ns1:title> (with accented e in detente)
                        doesn't match "Britain, Detente and Changing East-West Relations" in our DB.
                        '''
                        match_confidence = title_match_ratio(known_title, contributor_title)
                        match_confidences["title"] = match_confidence
                        if match_confidence > 80:
                            match_confidences["total"] += 0.6 * match_confidence
                            # match is good enough, we can stop
                            break



    def alternate_name_forms_for_cluster(self, cluster):
        """Find all pseudonyms in the given cluster."""
        for tag in ('400', '700'):
            for data_field in self._xpath(
                    cluster, './/*[local-name()="datafield"][@dtype="MARC21"][@tag="%s"]' % tag):
                for potential_match in self._xpath(
                        data_field, '*[local-name()="subfield"][@code="a"]'):
                    yield potential_match.text


    def sort_names_for_cluster(self, cluster):
        """Find all sort names for the given cluster."""
        for tag in ('100', '110'):
            for data_field in self._xpath(
                    cluster, './/*[local-name()="datafield"][@dtype="MARC21"][@tag="%s"]' % tag):
                for potential_match in self._xpath(
                        data_field, '*[local-name()="subfield"][@code="a"]'):
                    yield potential_match.text


    def name_titles_for_cluster(self, cluster):
        """Find all sort names for the given cluster."""
        for tag in ('100', '110'):
            for data_field in self._xpath(
                    cluster, './/*[local-name()="datafield"][@dtype="MARC21"][@tag="%s"]' % tag):
                for potential_match in self._xpath(
                        data_field, '*[local-name()="subfield"][@code="c"]'):
                    yield potential_match.text


    def cluster_has_record_for_named_author(
            self, cluster, working_sort_name, working_display_name, contributor_data=None):
        """  Looks through the xml cluster for all fields that could indicate the 
        author's name.

        Don't short-circuit the xml parsing process -- if found an author name 
        match, keep parsing and see what else can find.

        :return: a dictionary containing description of xml field 
        that matched author name searched for.
        """
        match_confidences = {}
        if not contributor_data:
            contributor_data = ContributorData()

        # If we have a sort name to look for, and it's in this cluster's
        # sort names, great.
        if working_sort_name:
            for potential_match in self.sort_names_for_cluster(cluster):
                match_confidence = contributor_name_match_ratio(potential_match, working_sort_name)
                match_confidences["sort_name"] = match_confidence
                # fuzzy match filter may not always give a 100% match, so cap arbitrarily at 90% as a "sure match"
                if match_confidence > 90:
                    contributor_data.sort_name=potential_match
                    return match_confidences

        # If we have a display name to look for, and this cluster's
        # Wikipedia name converts to the display name, great.
        if working_display_name:
            wikipedia_name = self.extract_wikipedia_name(cluster)
            if wikipedia_name:
                contributor_data.wikipedia_name=wikipedia_name
                display_name = self.wikipedia_name_to_display_name(wikipedia_name)
                match_confidence = contributor_name_match_ratio(display_name, working_display_name)
                match_confidences["display_name"] = match_confidence
                if match_confidence > 90:
                    contributor_data.display_name=display_name
                    return match_confidences

        # If there are UNIMARC records, and every part of the UNIMARC
        # record matches the sort name or the display name, great.
        unimarcs = self._xpath(cluster, './/*[local-name()="datafield"][@dtype="UNIMARC"]')
        candidates = []
        for unimarc in unimarcs:
            (possible_given, possible_family,
             possible_extra, possible_sort_name) = self.extract_name_from_unimarc(unimarc)
            if working_sort_name:
                match_confidence = contributor_name_match_ratio(possible_sort_name, working_sort_name)
                match_confidences["unimarc"] = match_confidence
                if match_confidence > 90:
                    contributor_data.family_name=possible_sort_name
                    return match_confidences

            for name in (working_sort_name, working_display_name):
                if not name:
                    continue
                if (possible_given and possible_given in name
                    and possible_family and possible_family in name and (
                        not possible_extra or possible_extra in name)):
                    match_confidences["unimarc"] = 90
                    contributor_data.family_name=possible_family
                    return match_confidences

        # Last-ditch effort. Guess at the sort name and see if *that's* one
        # of the cluster sort names.
        if working_display_name and not working_sort_name:
            test_sort_name = display_name_to_sort_name(working_display_name)
            for potential_match in self.sort_names_for_cluster(cluster):
                match_confidence = contributor_name_match_ratio(potential_match, test_sort_name)
                match_confidences["guessed_sort_name"] = match_confidence
                if match_confidence > 90:
                    contributor_data.sort_name=potential_match
                    return match_confidences

        # OK, last last-ditch effort.  See if the alternate name forms (pseudonyms) are it.
        if working_sort_name:
            for potential_match in self.alternate_name_forms_for_cluster(cluster):
                match_confidence = contributor_name_match_ratio(potential_match, working_sort_name)
                match_confidences["alternate_name"] = match_confidence
                if match_confidence > 90:
                    contributor_data.family_name=potential_match
                    return match_confidences
        
        return match_confidences


    def order_candidates(self, contributor_candidates, working_sort_name, 
                        known_titles=None, strict=False):
        """
        Accepts a list of tuples, each tuple containing: 
        - a ContributorData object filled with VIAF id, display, sort, family, 
        and wikipedia names, or None on error.
        - a list of work titles ascribed to this Contributor.

        For each contributor, determines how likely that contributor is to 
        be the one being searched for (how well they correspond to the 
        working_sort_name and known_title.

        Assumes the contributor_candidates list was generated off an xml 
        that was is in popularity order.  I.e., the author id that 
        appears in most libraries when searching for working_sort_name is on top.
        Assumes the xml's order is preserved in the contributor_candidates list.

        :return: the list of tuples, ordered by percent match, in descending order 
        (top match first).
        """
        if not contributor_candidates:
            return contributor_candidates

        # Double-check that the candidate list is ordered by library
        # popularity, as it came from viaf
        contributor_candidates.sort(key=lambda c: c[1].get('library_popularity'))
        # Grab the most popular candidate.
        (contributor_data, match_confidences, contributor_titles) = contributor_candidates[0]

        # If the top library popularity candidate is a really bad name
        # match, then don't penalize the bottom popularity candidates
        # for being on the bottom.
        ignore_popularity = False
        if match_confidences.get("library_popularity") == 1:
            if ("sort_name" in match_confidences and
                match_confidences["sort_name"] < 50):
                # baaad match
                ignore_popularity = True

            if ("guessed_sort_name" in match_confidences and
                match_confidences["guessed_sort_name"] < 50):
                ignore_popularity = True

            if (("sort_name" not in match_confidences) and 
                ("guessed_sort_name" not in match_confidences)):
                ignore_popularity = True


        # higher score for better match, so to have best match first, do desc order.
        contributor_candidates.sort(
            key=lambda x: self.weigh_contributor(
                x, working_sort_name=working_sort_name,
                known_titles=known_titles, strict=strict,
                ignore_popularity=ignore_popularity
            ),
            reverse=True
        )
        return contributor_candidates


    def parse_multiple(
            self, xml, working_sort_name=None, working_display_name=None, page=1):
        """ Parse a VIAF response containing multiple clusters into 
        contributors and titles.

        working_sort_name and working_display_name pertain to the author name string that 
        we're trying to match in the xml list of clusters.

        page refers to pagination -- we can get 10 clusters at a time from VIAF, 
        so an author's name that matches 15 contributors in VIAF search, will need 
        2 pages (2 queries going out to VIAF).

        NOTE:  No longer performs quality judgements on whether the contributor found is good enough.

        :return: a list of tuples, each tuple containing: 
        - a ContributorData object filled with VIAF id, display, sort, family, 
        and wikipedia names, or None on error.
        - a dictionary of viaf cluster properties, with weights assigned to each based on how 
        well the item in the viaf cluster matches the search parameters passed.
        - a list of work titles ascribed to this Contributor.
        """

        # TODO: decide: handle timeouts gracefully here, or keep throwing exception?
        if not xml:
            return []

        tree = etree.fromstring(xml, parser=etree.XMLParser(recover=True))

        # NOTE:  we can get the total number of clusters that a viaf search could return with: 
        # numberOfRecords_tag = self._xpath1(tree, './/*[local-name()="numberOfRecords"]')
        # but it's cleaner to call parse 50 times and quit when it's done than pass around record limits.

        # each contributor_candidate entry contains 3 objects:
        # a contributor_data, a dictionary of search match confidence weights, 
        # and a list of metadata objects representing authored titles.
        contributor_candidates = []
        for cluster in self._xpath(tree, '//*[local-name()="VIAFCluster"]'):
            contributor_data, match_confidences, contributor_titles = self.extract_viaf_info(
                cluster, working_sort_name, working_display_name)
            
            if not contributor_data:
                continue

            # assume we asked for viaf feed, sorted with sortKeys=holdingscount
            match_confidences["library_popularity"] = (len(contributor_candidates)+1) + 10 * (page-1)
            if contributor_data.display_name or contributor_data.viaf:
                contributor_candidate = (contributor_data, match_confidences, contributor_titles)
                contributor_candidates.append(contributor_candidate)
            
        # We could not find any names or viaf ids for this author.
        return contributor_candidates


    def parse(self, xml, working_sort_name=None, working_display_name=None):
        """ Parse a VIAF response containing a single cluster.

        NOTE:  No longer performs quality judgements on whether the contributor found is good enough.

        :return: a ContributorData object filled with display, sort, family, 
        and wikipedia names, and a list of titles this author has written.
        Return None on error.
        """

        tree = etree.fromstring(xml, parser=etree.XMLParser(recover=True))
        return self.extract_viaf_info(
            tree, working_sort_name, working_display_name
        )


    def extract_wikipedia_name(self, cluster):
        """Extract Wiki name from a single VIAF cluster."""
        for source in self._xpath(cluster, './/*[local-name()="sources"]/*[local-name()="source"]'):
            if source.text.startswith("WKP|"):
                # This could be a Wikipedia page, which is great,or it
                # could be a Wikidata ID, which we don't want.
                potential_wikipedia = source.text[4:]
                if not self.wikidata_id.search(potential_wikipedia):
                    return potential_wikipedia


    def sort_names_by_popularity(self, cluster):
        sort_name_popularity = Counter()
        for possible_sort_name in self.sort_names_for_cluster(cluster):
            if possible_sort_name.endswith(","):
                possible_sort_name = possible_sort_name[:-1]
            sort_name_popularity[possible_sort_name] += 1
        return sort_name_popularity


    def extract_viaf_info(self, cluster, working_sort_name=None,
                          working_display_name=False):
        """ Extract name info from a single VIAF cluster.

        :return: a tuple containing: 
        - ContributorData object filled with display, sort, family, and wikipedia names.
        - dictionary of ways the xml cluster data matched the names searched for.
        - list of titles attributed to the contributor in the cluster.
        or Nones on error.
        """
        contributor_data = ContributorData()
        contributor_titles = []
        match_confidences = {}

        # Find out if one of the working names shows up in a name record.
        # Note: Potentially sets contributor_data.sort_name.
        match_confidences = self.cluster_has_record_for_named_author(
                cluster, working_sort_name, working_display_name,
                contributor_data
        )

        # Get the VIAF ID for this cluster, just in case we don't have one yet.
        viaf_tag = self._xpath1(cluster, './/*[local-name()="viafID"]')
        if viaf_tag is None:
            contributor_data.viaf = None
        else:
            contributor_data.viaf = viaf_tag.text

        # If we don't have a working sort name, find the most popular
        # sort name in this cluster and use it as the sort name.
        sort_name_popularity = self.sort_names_by_popularity(cluster)

        # Does this cluster have a Wikipedia page?
        contributor_data.wikipedia_name = self.extract_wikipedia_name(cluster)
        if contributor_data.wikipedia_name:
            contributor_data.display_name = self.wikipedia_name_to_display_name(contributor_data.wikipedia_name)
            working_display_name = contributor_data.display_name
            # TODO: There's a problem here when someone's record has a
            # Wikipedia page other than their personal page (e.g. for
            # a band they're in.)

        known_name = working_sort_name or working_display_name
        unimarcs = self._xpath(cluster, './/*[local-name()="datafield"][@dtype="UNIMARC"]')
        candidates = []
        for unimarc in unimarcs:
            (possible_given, possible_family,
             possible_extra, possible_sort_name) = self.extract_name_from_unimarc(unimarc)
            # Some part of this name must also show up in the original
            # name for it to even be considered. Otherwise it's a
            # better bet to try to munge the original name.
            for v in (possible_given, possible_family, possible_extra):
                if not v:
                    continue
                if not known_name or v in known_name:
                    self.log.debug(
                        "FOUND %s in %s", v, known_name
                    )
                    candidates.append((possible_given, possible_family, possible_extra))
                    if possible_sort_name:
                        if possible_sort_name.endswith(","):
                            possible_sort_name = possible_sort_name[:-1]
                        sort_name_popularity[possible_sort_name] += 1
                    break
            else:
                self.log.debug(
                    "EXCLUDED %s/%s/%s for lack of resemblance to %s",
                    possible_given, possible_family, possible_extra,
                    known_name
                )
                pass

        if sort_name_popularity and not contributor_data.sort_name:
            contributor_data.sort_name, ignore = sort_name_popularity.most_common(1)[0]

        if contributor_data.display_name:
            parts = contributor_data.display_name.split(" ")
            if len(parts) == 2:
                # Pretty clearly given name+family name.
                # If it gets more complicated than this we can't
                # be confident.
                candidates.append(parts + [None])

        display_nameparts = self.best_choice(candidates)
        if display_nameparts[1]: # Family name
            contributor_data.family_name = display_nameparts[1]

        contributor_data.display_name = contributor_data.display_name or self.combine_nameparts(*display_nameparts) or working_display_name


        # Now go through the title elements, and make a list.
        titles = self._xpath(cluster, './/*[local-name()="titles"]/*[local-name()="work"]/*[local-name()="title"]')
        for title in titles:
            contributor_titles.append(title.text)

        return contributor_data, match_confidences, contributor_titles


    def wikipedia_name_to_display_name(self, wikipedia_name):
        """ Convert 'Bob_Jones_(Author)' to 'Bob Jones'. """
        display_name = wikipedia_name.replace("_", " ")
        if ' (' in display_name:
            display_name = display_name[:display_name.rindex(' (')]
        return display_name


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
            self.log.debug(
                "POSSIBILITY: %s/%s/%s",
                given_name, family_name, name_extra
            )
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
        """Turn a UNIMARC tag into a 4-tuple:
         (given name, family name, extra, sort name)
        """
        data = dict()
        sort_name_in_progress = []
        for (code, key) in (
                ('a', 'family'),
                ('b', 'given'),
                ('c', 'extra'),
                ):
            value = self._xpath1(unimarc, 'ns2:subfield[@code="%s"]' % code)
            if value is not None and value.text:
                value = value.text
                value = self.remove_commas_from(value)
                sort_name_in_progress.append(value)
                data[key] = value
        return (data.get('given', None), data.get('family', None),
                data.get('extra', None), ", ".join(sort_name_in_progress))



class VIAFClient(object):

    LOOKUP_URL = 'http://viaf.org/viaf/%(viaf)s/viaf.xml'
    SEARCH_URL = 'http://viaf.org/viaf/search?query={scope}+all+%22{author_name}%22&sortKeys=holdingscount&maximumRecords={maximum_records:d}&startRecord={start_record:d}&httpAccept=text/xml'

    SUBDIR = "viaf"

    MEDIA_TYPE = Representation.TEXT_XML_MEDIA_TYPE
    REPRESENTATION_MAX_AGE = 60*60*24*30*6    # 6 months

    def __init__(self, _db):
        self._db = _db
        self.parser = VIAFParser()
        self.log = logging.getLogger("VIAF Client")

    @property
    def data_source(self):
        return DataSource.lookup(self._db, DataSource.VIAF)

    def process_contributor(self, contributor):
        """ Accepts a Contributor object, and asks VIAF for information on the contributor's name.
        Finds the VIAF cluster that's most likely to correspond to the passed-in contributor.

        Finds any possible duplicate Contributor objects in our database, and 
        updates them with the information gleaned from VIAF.

        :return: a ContributorData object filled with display, sort, family, and wikipedia names
        from VIAF or None on error.
        """
        if contributor.viaf:
            contributor_candidate = self.lookup_by_viaf(
                contributor.viaf, contributor.sort_name, contributor.display_name
            )
        else:
            known_titles = set()
            if contributor.contributions:
                for contribution in contributor.contributions:
                    if contribution.edition and contribution.edition.title:
                        known_titles.add(contribution.edition.title)

            contributor_candidate = self.lookup_by_name(
                sort_name=contributor.sort_name, display_name=contributor.display_name, 
                known_titles=list(known_titles)
            )
        if not contributor_candidate:
            # No good match was identified.
            return None

        (selected_candidate, match_confidences, contributor_titles) = contributor_candidate
        if selected_candidate.viaf is not None:
            # Is there already another contributor with this VIAF?
            earliest_duplicate = self._db.query(Contributor).\
                filter(Contributor.viaf==selected_candidate.viaf).\
                filter(Contributor.id!=contributor.id).first()
            if earliest_duplicate:
                if earliest_duplicate.display_name == selected_candidate.display_name:
                    selected_candidate.apply(earliest_duplicate)
                    contributor.merge_into(earliest_duplicate)
                    return
                else:
                    # TODO: This might be okay or it might be a
                    # problem we need to address. Whatever it is,
                    # don't merge the records. Instead, apply the VIAF
                    # data to the provided contributor, potentially
                    # creating an accursed duplicate.
                    self.log.warn(
                        "AVOIDING POSSIBLE SPURIOUS AUTHOR MERGE: %r => %r",
                        selected_candidate, earliest_duplicate
                    )
            selected_candidate.apply(contributor)

    def select_best_match(self, candidates, working_sort_name, known_titles=None):
        """Gets the best VIAF match from a series of potential matches

        Return a tuple containing the selected_candidate (a ContributorData
        object), a dict of match_confidences, and a list of titles by the
        contributor.

        :param known_titles: A list of titles we know this author wrote.
        """

        # Sort for the best match and select the first.
        candidates = self.parser.order_candidates(
            working_sort_name=working_sort_name,
            contributor_candidates=candidates, 
            known_titles=known_titles
        )
        if not candidates:
            return None

        (selected_candidate, match_confidences, contributor_titles) = candidates[0]

        if (not selected_candidate or "total" not in match_confidences or
            match_confidences["total"] < 70):
            # The best match is dubious. Best to avoid this.
            return None

        return selected_candidate, match_confidences, contributor_titles


    def lookup_name_title(self, viaf, do_get=None):
        url = self.LOOKUP_URL % dict(viaf=viaf)
        r, cached = Representation.get(
            self._db, url, do_get=do_get, max_age=self.REPRESENTATION_MAX_AGE
        )

        xml = r.content
        cluster = etree.fromstring(xml, parser=etree.XMLParser(recover=True))

        titles = []
        for potential_title in self.parser.name_titles_for_cluster(cluster):
            titles.append(potential_title)
        return titles



    def lookup_by_viaf(self, viaf, working_sort_name=None,
                       working_display_name=None, do_get=None):
        url = self.LOOKUP_URL % dict(viaf=viaf)
        r, cached = Representation.get(
            self._db, url, do_get=do_get, max_age=self.REPRESENTATION_MAX_AGE
        )

        xml = r.content
        return self.parser.parse(xml, working_sort_name, working_display_name)


    def lookup_by_name(self, sort_name, display_name=None, do_get=None,
                       known_titles=None):
        """
        Asks VIAF for a list of author clusters, matching the passed-in 
        author name.  Selects the cluster we deem the best match for 
        the author we mean.

        :param sort_name: Author name in Last, First format.
        :param display_name: Author name in First Last format.
        :param do_get: Ask Representation to use Http GET?
        :param known_titles: A list of titles we know this author wrote.
        :return: (selected_candidate, match_confidences, contributor_titles) for selected ContributorData.
        """
        author_name = sort_name or display_name
        # from OCLC tech support:
        # VIAF's SRU endpoint can only return a maximum number of 10 records
        # when the recordSchema is http://viaf.org/VIAFCluster
        maximum_records = 10 # viaf maximum that's not ignored
        page = 1
        contributor_candidates = []

        # limit ourselves to reading the first 500 viaf clusters, on the
        # assumption that search match quality is unlikely to be usable after that.
        for page in range (1, 51):
            start_record = 1 + maximum_records * (page-1)
            scope = 'local.personalNames'
            if is_corporate_name(author_name):
                scope = 'local.corporateNames'

            url = self.SEARCH_URL.format(
                scope=scope, author_name=author_name.encode("utf8"),
                maximum_records=maximum_records, start_record=start_record
            )
            representation, cached = Representation.get(
                self._db, url, do_get=do_get, max_age=self.REPRESENTATION_MAX_AGE
            )
            xml = representation.content

            candidates = self.parser.parse_multiple(xml, sort_name, display_name, page)
            if not any(candidates):
                # Delete the representation so it's not cached.
                self._db.query(Representation).filter(
                    Representation.id==representation.id
                ).delete()
                # We ran out of clusters, so we can relax and move on to
                # ordering the returned results
                break

            contributor_candidates.extend(candidates)
            page += 1

        best_match = self.select_best_match(candidates=contributor_candidates, 
            working_sort_name=author_name, known_titles=known_titles)

        return best_match


        
class MockVIAFClient(VIAFClient):

    def __init__(self, _db):
        super(MockVIAFClient, self).__init__(_db)
        self.log = logging.getLogger("Mocked VIAF Client")
        base_path = os.path.split(__file__)[0]
        self.resource_path = os.path.join(base_path, "tests", "files", "viaf")


    def get_data(self, filename):
        # returns contents of sample file as xml string
        path = os.path.join(self.resource_path, filename)
        data = open(path).read()
        return data


    def lookup_by_viaf(self, viaf, working_sort_name=None,
                       working_display_name=None, do_get=None):
        xml = self.get_data("mindy_kaling.xml")
        return self.parser.parse(xml, working_sort_name, working_display_name)



