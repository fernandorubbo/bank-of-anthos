# Copyright 2019 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Web service for handling linked user contacts.

Manages internal user contacts and external accounts.
"""

import atexit
import logging
import os
import re
import sys

from flask import Flask, jsonify, request
import bleach
import jwt
from pylibs.db.database_helper import DatabaseHelper
from sqlalchemy.exc import OperationalError, SQLAlchemyError

logging.basicConfig(level=os.environ.get('LOGLEVEL', 'INFO').upper())

APP = Flask(__name__)


@APP.route('/version', methods=['GET'])
def version():
    """
    Service version endpoint
    """
    return VERSION, 200


@APP.route('/ready', methods=['GET'])
def ready():
    """Readiness probe."""
    return 'ok', 200


@APP.route('/contacts/<username>', methods=['GET'])
def get_contacts(username):
    """Retrieve the contacts list for the authenticated user.
    This list is used for populating Payment and Deposit fields.

    Return: a list of contacts
    """
    auth_header = request.headers.get('Authorization')
    if auth_header:
        token = auth_header.split(" ")[-1]
    else:
        token = ''
    try:
        auth_payload = jwt.decode(token, key=PUBLIC_KEY, algorithms='RS256')
        if username != auth_payload['user']:
            raise PermissionError
        contacts_list = CONTACTS_DB.get_contacts(username)
        return jsonify(contacts_list), 200
    except (PermissionError, jwt.exceptions.InvalidTokenError):
        return jsonify({'msg': 'authentication denied'}), 401
    except SQLAlchemyError as err:
        logging.error(err)
        return jsonify({'error': 'failed to retrieve contacts list'}), 500


@APP.route('/contacts/<username>', methods=['POST'])
def add_contact(username):
    """Add a new favorite account to user's contacts list

    Fails if account or routing number are invalid
    or if label is not alphanumeric

    request fields:
    - account_num
    - routing_num
    - label
    - is_external
    """
    auth_header = request.headers.get('Authorization')
    if auth_header:
        token = auth_header.split(" ")[-1]
    else:
        token = ''
    try:
        auth_payload = jwt.decode(token, key=PUBLIC_KEY, algorithms='RS256')
        if username != auth_payload['user']:
            raise PermissionError

        req = {k: (bleach.clean(v) if isinstance(v, str) else v)
               for k, v in request.get_json().items()}
        _validate_new_contact(req)

        # Don't allow self reference
        if (req['account_num'] == auth_payload['acct'] and
                req['routing_num'] == LOCAL_ROUTING):
            return jsonify({'msg': 'may not add yourself to contacts'}), 409

        CONTACTS_DB.add_contact(username, req)

    except (PermissionError, jwt.exceptions.InvalidTokenError):
        return jsonify({'msg': 'authentication denied'}), 401
    except UserWarning as warn:
        return jsonify({'msg': str(warn)}), 400
    except SQLAlchemyError as err:
        logging.error(err)
        return jsonify({'error': 'failed to add contact'}), 500

    return jsonify({}), 201


def _validate_new_contact(req):
    logging.debug('validating add contact request: %s', str(req))
    # Check if required fields are filled
    fields = ('label',
              'account_num',
              'routing_num',
              'is_external')
    if any(f not in req for f in fields):
        raise UserWarning('missing required field(s)')

    # Validate account number (must be 10 digits)
    if not re.match(r'\A[0-9]{10}\Z', req['account_num']):
        raise UserWarning('invalid account number')
    # Validate routing number (must be 9 digits)
    if not re.match(r'\A[0-9]{9}\Z', req['routing_num']):
        raise UserWarning('invalid routing number')
    # Only allow external accounts to deposit
    if req['is_external'] and req['routing_num'] == LOCAL_ROUTING:
        raise UserWarning('invalid routing number')
    # Validate label
    # Must be >0 and <30 chars, alphanumeric and spaces, can't start with space
    if not re.match(r'^[0-9a-zA-Z][0-9a-zA-Z ]{0,29}$', req['label']):
        raise UserWarning('invalid account label')


@atexit.register
def _shutdown():
    """Executed when web app is terminated."""
    try:
        CONTACTS_DB.close()
    except NameError:
        # catch name error when DB_CONN not set up
        pass
    logging.info("Stopping flask.")
    logging.shutdown()


if __name__ == '__main__':
    for v in ['PORT',
              'VERSION',
              'PUB_KEY_PATH',
              'LOCAL_ROUTING_NUM',
              'ACCOUNTS_DB_URI']:
        if os.environ.get(v) is None:
            logging.error("error: environment variable %s not set", v)
            logging.shutdown()
            sys.exit(1)

    VERSION = os.environ.get('VERSION')
    LOCAL_ROUTING = os.environ.get('LOCAL_ROUTING_NUM')
    PUBLIC_KEY = open(os.environ.get('PUB_KEY_PATH'), 'r').read()

    # Configure database connection
    try:
        CONTACTS_DB = DatabaseHelper("SQL", os.environ.get("ACCOUNTS_DB_URI")).database
    except OperationalError:
        logging.critical("database connection failed")
        sys.exit(1)

    logging.info("Starting flask.")
    APP.run(debug=False, port=os.environ.get('PORT'), host='0.0.0.0')
