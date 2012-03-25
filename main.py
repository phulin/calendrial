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

from apiclient.discovery import build
from oauth2client.appengine import oauth2decorator_from_clientsecrets
from oauth2client.client import AccessTokenRefreshError
from google.appengine.api import memcache
from google.appengine.api.oauth import get_current_user
from google.appengine.ext import webapp
from google.appengine.ext.webapp import template
from google.appengine.ext.webapp.util import run_wsgi_app
from google.appengine.ext import db

# CLIENT_SECRETS, name of a file containing the OAuth 2.0 information for this
# application, including client_id and client_secret, which are found
# on the API Access tab on the Google APIs
# Console <http://code.google.com/apis/console>
CLIENT_SECRETS = os.path.join(os.path.dirname(__file__), 'client_secrets.json')

# Helpful message to display in the browser if the CLIENT_SECRETS file
# is missing.
MISSING_CLIENT_SECRETS_MESSAGE = """
<h1>Warning: Please configure OAuth 2.0</h1>
<p>
To make this sample run you will need to populate the client_secrets.json file
found at:
    </p>
<p>
<code>%s</code>.
</p>
<p>with information found on the <a
href="https://code.google.com/apis/console">APIs Console</a>.
</p>
""" % CLIENT_SECRETS

http = httplib2.Http(memcache)
service = build(serviceName='calendar', version='v3', http=http,
                developerKey=settings.developer_key)
decorator = oauth2decorator_from_clientsecrets(
                CLIENT_SECRETS,
                'https://www.googleapis.com/auth/calendar.readonly',
                MISSING_CLIENT_SECRETS_MESSAGE)

genpath = lambda s: os.path.join(os.path.dirname(__file__), s)

class Slice(db.Model):
    startDate = db.DateProperty(required=True)
    endDate = db.DateProperty(required=True)
    startTime = db.TimeProperty(required=True)
    endTime = db.TimeProperty(required=True)

class User(db.Model):
    user = db.UserProperty(required=True)

def getEvents(cid, http, startDate, endDate):
    response = service.events().list(
        calendarId = cid,
        timeMin = startDate.isoformat(),
        timeMax = endDate.isoformat(),
        singleEvents = True,
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
        for date in [ eStartDate + datetime.timedelta(days = x) for x in range(numDays - 1) ]:
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

def invert(events):
    inverted = list()
    first = events[0]
    if first[0].time() > datetime.time(0, 0, 1):
        beforeFirst = datetime.datetime.combine(first[0], datetime.time(0, 0, 1))
        inverted.append((beforeFirst, first[0]))

    for i in range(len(events) - 1):
        now = events[i]
        next = events[i + 1]
        inverted.append((now[1], next[0]))

    last = events[-1]
    if last[1].time() < datetime.time(23, 59, 59):
        afterLast = datetime.datetime.combine(last[1], datetime.time(23, 59, 59))
        inverted.append((last[1], afterLast))

    return inverted

def out(event):
    start, end = event
    return start.strftime("%x: %X") + " to " + end.strftime("%X")

def user_key(user):
    return db.Key.from_path('User', user.user_id())
def slice_key(user, guid):
    return db.Key.from_path('User', user.user_id(), 'Slice', guid)

# stolen from Python docs
class UTC(datetime.tzinfo):
    """UTC"""
    def utcoffset(self, dt): return datetime.timedelta(0)
    def tzname(self, dt): return "UTC"
    def dst(self, dt): return datetime.timedelta(0)

class MainHandler(webapp.RequestHandler):
    @decorator.oauth_required
    def get(self, guid):
        user = get_current_user()

        slice = db.get(slice_key(user, guid))
        startDT = datetime.datetime.combine(slice.startDate, slice.startTime)
        startDT = startDT.replace(tzinfo = UTC())
        endDT = datetime.datetime.combine(slice.endDate, slice.endTime)
        endDT = endDT.replace(tzinfo = UTC())

        http = decorator.http()
        cals = service.calendarList().list(minAccessRole='writer').execute(http)['items']
        cal_ids = [ cal['id'] for cal in cals ]
        eventsLists = [ getEvents(c, http, startDT, endDT) for c in cal_ids ]
        events = reduce(operator.add, eventsLists)
        start_end = [ (e['start']['dateTime'], e['end']['dateTime']) for e in events ]
        parsed = [ map(iso8601.parse_date, e) for e in start_end ]
        merged = mergeEvents(parsed) # merge adjacent events
        inverted = invert(merged) # availability, not busy
        splitDone = splitEvents(inverted) # split multi-day events
        # now we should be guaranteed to have only one-day blocks
        restricted = restrict(splitDone, slice.startTime, slice.endTime)
        for e in restricted:
            self.response.out.write(out(e) + "<br />")
        # variables = {
        # }
        # self.response.out.write(template.render(genpath('index.html'), variables))

class CreateHandler(webapp.RequestHandler):
    @decorator.oauth_required
    def post(self):
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

        guid = uuid.uuid4().hex
        user = get_current_user()

        dbUser = db.get(user_key(user))
        if not dbUser:
            dbUser = User(key_name = user.user_id(), user = user)
            dbUser.put()

        slice = Slice(parent = user_key(user),
                      startDate = getDate('startDate'),
                      endDate = getDate('endDate'),
                      startTime = getTime('startTime'),
                      endTime = getTime('endTime'),
                      key_name = guid)
        slice.put()

        self.redirect('/' + guid)

    def get(self):
        self.response.out.write(template.render(genpath('create.html'), {}))

def main():
    application = webapp.WSGIApplication(
        [
         ('/', CreateHandler),
         (r'/(.*)', MainHandler),
        ],
        debug=True)
    run_wsgi_app(application)


if __name__ == '__main__':
      main()
