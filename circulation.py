from nose.tools import set_trace
import sys

from sqlalchemy.orm.exc import (
    NoResultFound
)

import flask
from flask import Flask, url_for, redirect

from model import (
    DataSource,
    production_session,
    LicensePool,
    WorkIdentifier,
    Work,
    WorkFeed,
    )
from lane import Lane, Unclassified
from opensearch import OpenSearchDocument
from opds import (
    AcquisitionFeed,
    NavigationFeed,
    URLRewriter,
)
import urllib
from util import LanguageCodes

db = production_session()
app = Flask(__name__)
app.debug = True

DEFAULT_LANGUAGES = ['eng']

def languages_for_request():
    return languages_from_accept(flask.request.accept_languages)

def languages_from_accept(accept_languages):
    languages = []
    for locale, quality in accept_languages:
        language = LanguageCodes.iso_639_2_for_locale(locale)
        if language:
            languages.append(language)
    if not languages:
        languages = DEFAULT_LANGUAGES
    return languages

@app.route('/')
def index():    
    return redirect(url_for('.navigation_feed'))

@app.route('/lanes/')
def navigation_feed():
    languages = languages_for_request()
    feed = NavigationFeed.main_feed(Lane)

    feed.links.append(
        dict(rel="search",
             href=url_for('lane_search', lane=None, _external=True)))
    return unicode(feed)

def lane_url(cls, lane, order=None):
    if isinstance(lane, Lane):
        lane = lane.name
    return url_for('feed', lane=lane, order=order, _external=True)


@app.route('/lanes/<lane>')
def feed(lane):
    languages = languages_for_request()
    arg = flask.request.args.get
    order = arg('order', 'recommended')
    last_seen_id = arg('last_seen', None)

    search_link = dict(
        rel="search",
        href=url_for('lane_search', lane=lane, _external=True))

    if order == 'recommended':
        feed = AcquisitionFeed.featured(db, languages, lane)
        feed.links.append(search_link)
        return unicode(feed)

    if order == 'title':
        feed = WorkFeed(languages, lane, Work.title)
        title = "%s: By title" % lane
    elif order == 'author':
        feed = WorkFeed(languages, lane, Work.authors)
        title = "%s: By author" % lane
    else:
        return "I don't know how to order a feed by '%s'" % order

    size = arg('size', '50')
    try:
        size = int(size)
    except ValueError:
        return "Invalid size: %s" % size
    size = max(size, 10)
    size = min(size, 100)

    last_work_seen = None
    last_id = arg('after', None)
    if last_id:
        try:
            last_id = int(last_id)
        except ValueError:
            return "Invalid work ID: %s" % last_id
        try:
            last_work_seen = db.query(Work).filter(Work.id==last_id).one()
        except NoResultFound:
            return "No such work id: %s" % last_id

    this_url = url_for('feed', lane=lane, order=order, _external=True)
    page = feed.page_query(db, last_work_seen, size).all()
    url_generator = lambda x : url_for(
        'feed', lane=lane, order=x, _external=True)

    opds_feed = AcquisitionFeed(db, title, this_url, page, url_generator)
    # Add a 'next' link if appropriate.
    if page and len(page) >= size:
        after = page[-1].id
        next_url = url_for(
            'feed', lane=lane, order=order, after=after, _external=True)
        opds_feed.links.append(dict(rel="next", href=next_url))

    opds_feed.links.append(search_link)
    return unicode(opds_feed)

@app.route('/search', defaults=dict(lane=None))
@app.route('/search/<lane>')
def lane_search(lane):
    languages = languages_for_request()
    query = flask.request.args.get('q')
    this_url = url_for('lane_search', lane=lane, _external=True)
    if not query:
        # Send the search form
        return OpenSearchDocument.for_lane(lane, this_url)
    # Run a search.
    results = Work.search(db, query, languages, lane).limit(50)
    info = OpenSearchDocument.search_info(lane)
    opds_feed = AcquisitionFeed(
        db, info['name'], 
        this_url + "?q=" + urllib.quote(query),
        results)
    return unicode(opds_feed)

@app.route('/works/<data_source>/<identifier>/checkout')
def checkout(data_source, identifier):

    # Turn source + identifier into a LicensePool
    source = DataSource.lookup(db, data_source)
    if source is None:
        return "No such data source!"
    identifier_type = source.primary_identifier_type

    id_obj, ignore = WorkIdentifier.for_foreign_id(
        db, identifier_type, identifier, autocreate=False)
    if not id_obj:
        # TODO
        return "I never heard of such a book."

    pool = id_obj.licensed_through
    if not pool:
        return "I don't have any licenses for that book."

    best_pool, best_link = pool.best_license_link
    if not best_link:
        return "Sorry, couldn't find an available license."
    
    return redirect(URLRewriter.rewrite(best_link))

print __name__
if __name__ == '__main__':

    debug = True
    host = "0.0.0.0"
    app.run(debug=debug, host=host)
