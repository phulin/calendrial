#!/usr/bin/env python
#
# Copyright 2007 Google Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
"""calendrial, an application for better scheduling.
"""

__author__ = 'patrick.hulin@gmail.com (Patrick Hulin)'

import settings

import httplib2
import logging
import os
import pickle
import operator
import datetime
import iso8601
import uuid
import hashlib

from apiclient.discovery import build
from oauth2client.client import AccessTokenRefreshError, OAuth2WebServerFlow
from oauth2client.clientsecrets import loadfile
from oauth2client.appengine import CredentialsProperty
from google.appengine.api import memcache
from google.appengine.api.oauth import get_current_user
from google.appengine.ext import webapp
from google.appengine.ext.webapp import template
from google.appengine.ext.webapp.util import run_wsgi_app, login_required
from google.appengine.ext import db

# Canonical date and time formats. Ignoring localization.
dateFormat = "%A, %B %d"
timeFormat = "%I:%M %p"

# CLIENT_SECRETS, name of a file containing the OAuth 2.0 information for this
# application, including client_id and client_secret, which are found
# on the API Access tab on the Google APIs
# Console <http://code.google.com/apis/console>
CLIENT_SECRETS = os.path.join(os.path.dirname(__file__), 'client_secrets.json')

http = httplib2.Http(memcache)

# decorator = oauth2decorator_from_clientsecrets(
#                 CLIENT_SECRETS,
#                 'https://www.googleapis.com/auth/calendar.readonly',
#                 MISSING_CLIENT_SECRETS_MESSAGE)

genpath = lambda s: os.path.join(os.path.dirname(__file__), s)

class Slice(db.Model):
    startDate = db.DateProperty(required=True)
    endDate = db.DateProperty(required=True)
    startTime = db.TimeProperty(required=True)
    endTime = db.TimeProperty(required=True)

class User(db.Model):
    user = db.UserProperty(required=True)
    credentials = CredentialsProperty()
    guid = db.StringProperty()
    guidExpiry = db.DateTimeProperty()

def getEvents(cid, service, http, startDate, endDate, timeZone):
    response = service.events().list(
        calendarId = cid,
        timeMin = startDate.isoformat(),
        timeMax = endDate.isoformat(),
        singleEvents = True,
        timeZone = timeZone
    ).execute(http)
    if 'items' in response:
        return response['items']
    else:
        return []

# split multi-day events
def splitEvents(events):
    return reduce(operator.add, map(splitEvent, events))

def splitEvent(e):
    if e[0].date() == e[1].date():
        return [e]
    else:
        result = list()

        eStartDate = e[0].date()
        eEndDate = e[1].date()

        firstEnd = datetime.datetime.combine(eStartDate, datetime.time(23, 59, 59))
        result.append((e[0], firstEnd))

        numDays = (eEndDate - eStartDate).days
        for date in [ eStartDate + datetime.timedelta(days = x) for x in range(1, numDays) ]:
            start = datetime.datetime.combine(date, datetime.time(0, 0, 1))
            end = datetime.datetime.combine(date, datetime.time(23, 59, 59))
            result.append((start, end))

        lastStart = datetime.datetime.combine(eEndDate, datetime.time(0, 0, 1))
        result.append((lastStart, e[1]))
    return result

# assume no events span days, i.e. call splitEvents first
def restrict(parsed, startTime, endTime):
    new = list()
    for e in parsed:
        eStart = e[0].time()
        eEnd = e[1].time()
        if eEnd > startTime and eStart < endTime:
            if eStart < startTime: eStart = startTime
            if eEnd > endTime: eEnd = endTime
            newStart = datetime.datetime.combine(e[0].date(), eStart)
            newEnd = datetime.datetime.combine(e[1].date(), eEnd)
            new.append((newStart, newEnd))
    return new

def mergeEvents(parsed):
    ordered = sorted(parsed, key = lambda x: x[0])
    return reduce(mergeEvent, ordered, [])

# merge one event into a list
def mergeEvent(events, e):
    if len(events) == 0:
        return [e]
    last = events[-1]
    if last[1] < e[0]:
        events.append(e)
    else:
        events[-1] = (last[0], max(e[1], last[1]))
    return events

def invert(events, startDate, endDate, tzinfo):
    inverted = list()

    first = events[0]
    startStart = datetime.datetime.combine(startDate, datetime.time(0, 0, 1))
    startStart = startStart.replace(tzinfo = tzinfo)
    if first[0] > startStart:
        inverted.append((startStart, first[0]))

    for i in range(len(events) - 1):
        now = events[i]
        next = events[i + 1]
        inverted.append((now[1], next[0]))

    last = events[-1]
    endEnd = datetime.datetime.combine(endDate, datetime.time(23, 59, 59))
    endEnd = endEnd.replace(tzinfo = tzinfo)
    if last[1] < endEnd:
        inverted.append((last[1], endEnd))

    return inverted

# takes list of tuples of (start, end)
# returns list of tuples of [(date, [(startTime, endTime)])]
def groupEvents(events):
    return reduce(groupEvent, events, [])
def groupEvent(events, e):
    timeTuple = (e[0].time(), e[1].time())

    if len(events) == 0:
        return [(e[0].date(), [timeTuple])]

    lastDate, lastTimes = events[-1]
    if lastDate == e[0].date():
        lastTimes.append(timeTuple)
    else:
        events.append((e[0].date(), [timeTuple]))

    return events

def dayOut(dayTuple):
    date, timeList = dayTuple
    dateString = date.strftime(dateFormat + ": ")
    timeStrings = []
    for (startTime, endTime) in timeList:
        timeStrings.append(startTime.strftime(timeFormat) + " to " + endTime.strftime(timeFormat))
    return dateString + reduce(lambda x, y: x + ", " + y, timeStrings)

def mkUserHash(user):
    return hashlib.md5(user.user_id()).hexdigest()[:8]
def userKey(userHash):
    return db.Key.from_path('User', userHash)
def sliceKey(userHash, guid):
    return db.Key.from_path('User', userHash, 'Slice', guid)

class StringTZInfo(datetime.tzinfo):
    """tzinfo class from string"""
    def __init__(self, s):
        self.s = s
        pm = s[0]
        hour = int(s[1:3])
        minute = int(s[3:5])
        if pm == "+":
            self.delta = datetime.timedelta(hours = hour, minutes = minute)
        else:
            self.delta = datetime.timedelta(hours = - hour, minutes = minute)

    def utcoffset(self, dt): return self.delta
    def dst(self, dt): return self.delta
    def tzname(self, dt): return self.s

class MainHandler(webapp.RequestHandler):
    def get(self, userHash, guid, timeZone = None):
        def linesOut(xs):
            for x in xs:
                self.response.out.write(str(x) + "<br />")

        if not timeZone:
            timeZone = "-0400"
        tzinfo = StringTZInfo(timeZone)

        slice = db.get(sliceKey(userHash, guid))
        if slice is None:
            self.response.out.write("No such slice (userHash: %s, guid: %s)." % (userHash, guid))
            return
        # interpret given times as being in the given time zone
        startDT = datetime.datetime.combine(slice.startDate, slice.startTime)
        startDT = startDT.replace(tzinfo = tzinfo)
        endDT = datetime.datetime.combine(slice.endDate, slice.endTime)
        endDT = endDT.replace(tzinfo = tzinfo)

        dbUser = db.get(userKey(userHash))
        if not dbUser:
            raise ValueError("No user")
        user = dbUser.user
        credentials = dbUser.credentials
        if not credentials:
            raise ValueError("No credentials")

        http = httplib2.Http()
        http = credentials.authorize(http)
        if credentials.access_token_expired:
            logging.debug("get: access token expired, refreshing")
            credentials.refresh(http)
            http = credentials.authorize(http)

        service = build(
                serviceName='calendar', version='v3', http=http,
                developerKey=settings.developer_key
        )
        calendarList = service.calendarList()
        response = calendarList.list(
                minAccessRole='writer'
        ).execute(http)
        cals = response['items']

        calIds = [ cal['id'] for cal in cals ]
        eventsLists = [ getEvents(c, service, http, startDT, endDT, timeZone) for c in calIds ]
        events = reduce(operator.add, eventsLists)
        startEnd = [ (e['start']['dateTime'], e['end']['dateTime']) for e in events]
        parsed = [ map(iso8601.parse_date, e) for e in startEnd ]
        merged = mergeEvents(parsed) # merge adjacent events
        inverted = invert(merged, slice.startDate, slice.endDate, tzinfo) # availability, not busy
        splitDone = splitEvents(inverted) # split multi-day events
        # now we should be guaranteed to have only one-day blocks
        restricted = restrict(splitDone, slice.startTime, slice.endTime)
        dayGrouped = groupEvents(restricted)

        variables = {
            'days': map(dayOut, dayGrouped),
            'startDate': slice.startDate.strftime(dateFormat),
            'endDate': slice.endDate.strftime(dateFormat),
            'startTime': slice.startTime.strftime(timeFormat),
            'endTime': slice.endTime.strftime(timeFormat),
            'timeZone': timeZone,
            'nickname': user.nickname(),
            'email': user.email()
        }
        self.response.out.write(template.render(genpath('index.html'), variables))

    def post(self, userHash, guid):
        def getDate(s):
            dateString = self.request.get(s)
            format = "%Y-%m-%d"
            return datetime.datetime.strptime(dateString, format).date()
        def getTime(s):
            dateString = self.request.get(s)
            if dateString == "00:00:00":
                return datetime.time(0, 0, 1)
            format = "%H:%M:%S"
            dt = datetime.datetime.strptime(dateString, format)
            return dt.time()

        dbUser = db.get(userKey(userHash))
        if not dbUser:
            raise ValueError('No such user')
        # TODO: replace these with real templates
        if dbUser.guid != guid:
            self.response.out.write('invalid guid')
            return
        if dbUser.guidExpiry <= datetime.datetime.now():
            self.response.out.write('guid expired')
            return
        # revoke secret
        # dbUser.guid = None
        # dbUser.guidExpiry = None
        # dbUser.put()

        slice = Slice(
                parent = userKey(userHash),
                startDate = getDate('startDate'),
                endDate = getDate('endDate'),
                startTime = getTime('startTime'),
                endTime = getTime('endTime'),
                key_name = guid
        )
        slice.put()
        redirectUrl = '/' + userHash + '/' + guid
        timeZone = self.request.get('timeZone')
        if timeZone is not "":
            redirectUrl += '/' + timeZone

        if not dbUser.credentials:
            raise ValueError("No credentials while posting")
        self.redirect(redirectUrl)

class CreateHandler(webapp.RequestHandler):
    @login_required
    def get(self):
        user = get_current_user()
        guid = uuid.uuid4().hex[:8]

        dbUser = db.get(userKey(mkUserHash(user)))
        if not dbUser:
            logging.debug('making new user')
            dbUser = User(
                    key_name = mkUserHash(user),
                    user = user,
            )
        dbUser.guid = guid
        dbUser.guidExpiry = datetime.datetime.now() + datetime.timedelta(minutes = 10)
        dbUser.put()

        redirectUrl = '/'
        def requestToken():
            logging.debug('requesting new access token')
            xxxx, clientInfo = loadfile(CLIENT_SECRETS)
            flow = OAuth2WebServerFlow(
                    client_id = clientInfo['client_id'],
                    client_secret = clientInfo['client_secret'],
                    scope = 'https://www.googleapis.com/auth/calendar.readonly',
                    user_agent = 'calendrial/0.0',
                    access_type = 'offline'
            )
            callback = self.request.relative_url('/oauth2callback')
            authorizeUrl = flow.step1_get_authorize_url(callback)
            memcache.set(user.user_id(), pickle.dumps(flow))
            memcache.set(user.user_id() + "_url", redirectUrl)
            self.redirect(authorizeUrl)

        credentials = dbUser.credentials
        if credentials is None:
            requestToken()
            return
        else:
            if credentials.access_token_expired:
                logging.debug('refreshing access token')
                try:
                    http = httplib2.Http()
                    http = credentials.authorize(http)
                    credentials.refresh(http)
                except AccessTokenRefreshError:
                    logging.debug("refresh failed.")
                    requestToken()
                    return

        variables = {
                'userHash': mkUserHash(user),
                'userId': user.user_id(),
                'guid': guid
        }
        self.response.out.write(template.render(genpath('create.html'), variables))

class MyOAuthHandler(webapp.RequestHandler):
    @login_required
    def get(self):
        user = get_current_user()
        pickledFlow = memcache.get(user.user_id())
        if not pickledFlow:
            raise ValueError("Flow not in cache")
        flow = pickle.loads(memcache.get(user.user_id()))
        if flow:
            credentials = flow.step2_exchange(self.request.params)
            dbUser = db.get(userKey(mkUserHash(user)))            
            if not dbUser:
                raise ValueError("Shouldn't be creating dbUsers in callback.")
                # dbUser = User(key_name = mkUserHash(user), user = user)
            dbUser.credentials = credentials
            dbUser.put()
            self.redirect(memcache.get(user.user_id() + '_url'))
        else:
            raise ValueError("No flow")

def main():
    application = webapp.WSGIApplication(
        [
         ('/', CreateHandler),
         ('/oauth2callback', MyOAuthHandler),
         (r'/([0-9a-f]{8})/([0-9a-f]{8})/(.+)', MainHandler),
         (r'/([0-9a-f]{8})/([0-9a-f]{8})', MainHandler),
        ],
        debug=True)
    run_wsgi_app(application)


if __name__ == '__main__':
      main()
