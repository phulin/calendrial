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
from datetime import datetime
import iso8601

from apiclient.discovery import build
from oauth2client.appengine import oauth2decorator_from_clientsecrets
from oauth2client.client import AccessTokenRefreshError
from google.appengine.api import memcache
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
    guid = StringProperty(required=True)
    startDate = DateProperty(required=True)
    endDate = DateProperty(required=True)
    startTime = DateProperty(required=True)
    endTime = DateProperty(required=True)

def getEvents(cid, http):
    response = service.events().list(
        calendarId = cid,
        timeMin = '2012-03-20T00:00:00Z',
        timeMax = '2012-04-01T00:00:00Z',
        singleEvents = True,
    ).execute(http)
    if 'items' in response:
        return response['items']
    else:
        return []

def restrict(parsed, startTime, endTime):
    new = list()
    for e in parsed:
        if e[0] > startTime or e[1] < endTime:
            if e[0] < startTime: e[0] = startTime
            if e[1] > endTime: e[1] = endTime
            new.append(e)
    return new

class MainHandler(webapp.RequestHandler):
    @decorator.oauth_required
    def post(self):

    @decorator.oauth_required
    def get(self):
        http = decorator.http()
        cals = service.calendarList().list(minAccessRole='writer').execute(http)['items']
        cal_ids = [cal['id'] for cal in cals]
        events = reduce(operator.add, [getEvents(c, http) for c in cal_ids])
        start_end = [(e['start']['dateTime'], e['end']['dateTime']) for e in events]
        parsed = [map(iso8601.parse_date, e) for e in start_end]
        self.response.out.write(parsed)
        variables = {
        }
        # self.response.out.write(template.render(genpath('index.html'), variables))

def main():
    application = webapp.WSGIApplication(
        [
         ('/', MainHandler),
        ],
        debug=True)
    run_wsgi_app(application)


if __name__ == '__main__':
      main()
