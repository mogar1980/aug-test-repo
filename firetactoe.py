# Copyright 2016 Google Inc. All Rights Reserved.

"""Tic Tac Toe with the Firebase API"""

import datetime
from functools import wraps
import json
import os
import re
import urllib

from Crypto.PublicKey import RSA
import flask
from flask import request
from google.appengine.api import users
from google.appengine.ext import db
import jwt
import requests
import requests_toolbelt.adapters.appengine

# Use the App Engine Requests adapter. This makes sure that Requests uses
# URLFetch.
requests_toolbelt.adapters.appengine.monkeypatch()


# Firebase database server authorization key
# This is used to authenticate the server and authorize write access
#
# Replace this value with your Firebase db key from:
#
#     https://console.firebase.google.com/project/_/settings/database
#
FIRE_DB_KEY = 'REPLACE_WITH_YOUR_DB_KEY'

_FIREBASE_CONFIG_TEMPLATE = '_firebase_config.html'
_SERVICE_ACCOUNT_FILENAME = 'credentials.json'

_CWD = os.path.dirname(__file__)
_IDENTITY_ENDPOINT = ('https://identitytoolkit.googleapis.com/'
                      'google.identity.identitytoolkit.v1.IdentityToolkit')
# Firebase database url/channels memoization
_FIRE_URL = None


app = flask.Flask(__name__)


def _get_firebase_config():
    """Parses the client-side Firebase config into a dict."""
    with open(os.path.join(_CWD, 'templates', _FIREBASE_CONFIG_TEMPLATE)) as f:
        html = f.read()
    obj = re.search(r'{([^}]+)}', html, re.MULTILINE)
    params = [p.strip().split(':', 1) for p in obj.group(1).split(',')]
    return dict((k.strip('" '), v.strip('" ')) for k, v in params)


def _send_firebase_message(u_id, message=None):
    global _FIRE_URL
    if not _FIRE_URL:
        config = _get_firebase_config()
        _FIRE_URL = '{}/channels/{{}}.json?auth={}'.format(
            config['databaseURL'], FIRE_DB_KEY)

    if message:
        return requests.patch(
            _FIRE_URL.format(u_id), data=message).json()
    else:
        return requests.delete(_FIRE_URL.format(u_id)).json()


def create_custom_token(uid):
    """Create a secure token for the given id.

    This method is used to create secure custom tokens to be passed to clients
    it takes a unique id (uid) that will be used by Firebase's security rules
    to prevent unauthorized access. In this case, the uid will be the channel
    id which is a combination of user_id and game_key
    """
    with open(os.path.join(_CWD, _SERVICE_ACCOUNT_FILENAME), 'r') as f:
        credentials = json.load(f)

    payload = {
        'iss': credentials['client_email'],
        'sub': credentials['client_email'],
        'aud': _IDENTITY_ENDPOINT,
        'uid': uid,
    }
    exp = datetime.timedelta(minutes=60)
    return jwt.generate_jwt(
        payload, RSA.importKey(credentials['private_key']), 'RS256', exp)


class Wins():
    """A collection of patterns of winning boards."""
    x_win_patterns = ['XXX......',
                      '...XXX...',
                      '......XXX',
                      'X..X..X..',
                      '.X..X..X.',
                      '..X..X..X',
                      'X...X...X',
                      '..X.X.X..']
    o_win_patterns = map(lambda s: s.replace('X', 'O'), x_win_patterns)

    x_wins = map(lambda s: re.compile(s), x_win_patterns)
    o_wins = map(lambda s: re.compile(s), o_win_patterns)


class Game(db.Model):
    """All the data we store for a game"""
    userX = db.UserProperty()
    userO = db.UserProperty()
    board = db.StringProperty()
    moveX = db.BooleanProperty()
    winner = db.StringProperty()
    winning_board = db.StringProperty()

    def to_json(self):
        d = db.to_dict(self)
        d['winningBoard'] = d.pop('winning_board')
        return json.dumps(d, default=lambda user: user.user_id())

    def send_update(self):
        """Updates Firebase's copy of the board."""
        message = self.to_json()
        # send updated game state to user X
        _send_firebase_message(
            self.userX.user_id() + self.key().id_or_name(),
            message=message)
        # send updated game state to user O
        if self.userO:
            _send_firebase_message(
                self.userO.user_id() + self.key().id_or_name(),
                message=message)

    def _check_win(self):
        if self.moveX:
            # O just moved, check for O wins
            wins = Wins.o_wins
            potential_winner = self.userO.user_id()
        else:
            # X just moved, check for X wins
            wins = Wins.x_wins
            potential_winner = self.userX.user_id()

        for win in wins:
            if win.match(self.board):
                self.winner = potential_winner
                self.winning_board = win.pattern
                return

        # In case of a draw, everyone loses.
        if ' ' not in self.board:
            self.winner = 'Noone'

    def make_move(self, position, user):
        if (position >= 0 and user == self.userX) or (
                user == self.userO):
            if self.moveX == (user == self.userX):
                boardList = list(self.board)
                if (boardList[position] == ' '):
                    boardList[position] = 'X' if self.moveX else 'O'
                    self.board = ''.join(boardList)
                    self.moveX = not self.moveX
                    self._check_win()
                    self.put()
                    self.send_update()
                    return


def login_required(f):
    """Decorator to enforce logged-in state."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not users.get_current_user():
            return flask.redirect(users.create_login_url(request.full_path))
        return f(*args, **kwargs)
    return decorated_function


@app.route('/move', methods=['POST'])
@login_required
def move():
    game = Game.get_by_key_name(request.args.get('g'))
    if not game:
        return 'Game not found', 400
    game.make_move(int(request.args.get('i')), users.get_current_user())
    return ''


@app.route('/delete', methods=['POST'])
@login_required
def delete():
    game = Game.get_by_key_name(request.args.get('g'))
    if not game:
        return 'Game not found', 400
    user = users.get_current_user()
    _send_firebase_message(
        user.user_id() + game.key().id_or_name(), message=None)
    return ''


@app.route('/opened', methods=['POST'])
@login_required
def opened():
    game = Game.get_by_key_name(request.args.get('g'))
    if not game:
        return 'Game not found', 400
    game.send_update()
    return ''


@app.route('/')
@login_required
def main_page():
    """Renders the main page. When this page is shown, we create a new
    channel to push asynchronous updates to the client."""
    user = users.get_current_user()
    game_key = request.args.get('g')

    if not game_key:
        game_key = user.user_id()
        game = Game(
            key_name=game_key, userX=user, moveX=True,
            board=' '*9)
        game.put()
    else:
        game = Game.get_by_key_name(game_key)
        if not game:
            return 'No such game', 404
        if not game.userO:
            game.userO = user
            game.put()

    # choose a unique identifier for channel_id
    channel_id = user.user_id() + game_key
    # encrypt the channel_id and send it as a custom token to the
    # client
    # Firebase's data security rules will be able to decrypt the
    # token and prevent unauthorized access
    client_auth_token = create_custom_token(channel_id)
    _send_firebase_message(
        channel_id, message=game.to_json())

    game_link = '{}?g={}'.format(request.base_url, game_key)

    # push all the data to the html template so the client will
    # have access
    template_values = {
        'token': client_auth_token,
        'channel_id': channel_id,
        'me': user.user_id(),
        'game_key': game_key,
        'game_link': game_link,
        'initial_message': urllib.unquote(game.to_json())
    }

    return flask.render_template('fire_index.html', **template_values)
