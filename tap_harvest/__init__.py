#!/usr/bin/env python3

import os
import time
from datetime import datetime

import backoff
import requests
import pendulum

import singer
from singer import Transformer, utils

LOGGER = singer.get_logger()
SESSION = requests.Session()
REQUIRED_CONFIG_KEYS = [
    "start_date",
    "refresh_token",
    "client_id",
    "client_secret",
    "user_agent",
]

BASE_API_URL = "https://api.harvestapp.com/v2/"
BASE_ID_URL = "https://id.getharvest.com/api/v2/"
CONFIG = {}
STATE = {}
AUTH = {}

ALL_STREAMS = ['clients', 'contacts', 'projects', 'tasks', 'project_tasks', 'project_users', 'expense_categories',
               'invoice_item_categories', 'estimate_item_categories', 'user_roles', 'external_reference', 'time_entry_external_reference',
               'time_entries', 'invoice_messages', 'invoice_payments', 'invoice_line_items', 'invoices', 'estimate_line_items', 'estimates',
               'user_projects', 'user_project_tasks', 'users', 'expenses']


class Auth:
    def __init__(self, client_id, client_secret, refresh_token):
        self._client_id = client_id
        self._client_secret = client_secret
        self._refresh_token = refresh_token
        self._account_id = None
        self._refresh_access_token()

    @backoff.on_exception(
        backoff.expo,
        requests.exceptions.RequestException,
        max_tries=5,
        giveup=lambda e: e.response is not None and 400 <= e.response.status_code < 500,
        factor=2)
    def _make_refresh_token_request(self):
        return requests.request('POST',
                                url=BASE_ID_URL + 'oauth2/token',
                                data={
                                    'client_id': self._client_id,
                                    'client_secret': self._client_secret,
                                    'refresh_token': self._refresh_token,
                                    'grant_type': 'refresh_token',
                                },
                                headers={"User-Agent": CONFIG.get("user_agent")})

    def _refresh_access_token(self):
        LOGGER.info("Refreshing access token")
        resp = self._make_refresh_token_request()
        expires_in_seconds = resp.json().get('expires_in', 17 * 60 * 60)
        self._expires_at = pendulum.now().add(seconds=expires_in_seconds)
        resp_json = {}
        try:
            resp_json = resp.json()
            self._access_token = resp_json['access_token']
        except KeyError as key_err:
            if resp_json.get('error'):
                LOGGER.critical(resp_json.get('error'))
            if resp_json.get('error_description'):
                LOGGER.critical(resp_json.get('error_description'))
            raise key_err
        LOGGER.info("Got refreshed access token")

    def get_access_token(self):
        if self._access_token is not None and self._expires_at > pendulum.now():
            return self._access_token

        self._refresh_access_token()
        return self._access_token

    def get_account_id(self):
        if self._account_id is not None:
            return self._account_id

        response = requests.request('GET',
                                    url=BASE_ID_URL + 'accounts',
                                    headers={'Authorization': 'Bearer ' + self._access_token,
                                             'User-Agent': CONFIG.get("user_agent")})

        self._account_id = str(response.json()['accounts'][0]['id'])

        return self._account_id


def get_abs_path(path):
    return os.path.join(os.path.dirname(os.path.realpath(__file__)), path)


def load_schema(entity):
    return utils.load_json(get_abs_path("schemas/{}.json".format(entity)))


def load_and_write_schema(name, key_properties='id', bookmark_property='updated_at'):
    schema = load_schema(name)
    singer.write_schema(name, schema, key_properties, bookmark_properties=[bookmark_property])
    return schema


def get_start(key):
    if key not in STATE:
        STATE[key] = CONFIG['start_date']

    return STATE[key]


def get_url(endpoint):
    return BASE_API_URL + endpoint


@backoff.on_exception(
    backoff.expo,
    requests.exceptions.RequestException,
    max_tries=5,
    giveup=lambda e: e.response is not None and 400 <= e.response.status_code < 500,
    factor=2)
@utils.ratelimit(100, 15)
def request(url, params=None):
    params = params or {}
    access_token = AUTH.get_access_token()
    headers = {"Accept": "application/json",
               "Harvest-Account-Id": AUTH.get_account_id(),
               "Authorization": "Bearer " + access_token,
               "User-Agent": CONFIG.get("user_agent")}
    req = requests.Request("GET", url=url, params=params, headers=headers).prepare()
    LOGGER.info("GET {}".format(req.url))
    resp = SESSION.send(req)
    resp.raise_for_status()
    return resp.json()


# Any date-times values can either be a string or a null.
# If null, parsing the date results in an error.
# Instead, removing the attribute before parsing ignores this error.
def remove_empty_date_times(item, schema):
    fields = []

    for key in schema['properties']:
        subschema = schema['properties'][key]
        if subschema.get('format') == 'date-time':
            fields.append(key)

    for field in fields:
        if item.get(field) is None:
            del item[field]


def append_times_to_dates(item, date_fields):
    if date_fields:
        for date_field in date_fields:
            if item.get(date_field):
                item[date_field] = utils.strftime(utils.strptime_with_tz(item[date_field]))


def get_company():
    url = get_url('company')
    return request(url)


def get_stream_version():
    full_replication = CONFIG.get("full_replication", False)
    if full_replication:
        return int(datetime.now().timestamp())

    return 1

def sync_endpoint(schema_name, endpoint=None, path=None, date_fields=None, with_updated_since=True, #pylint: disable=too-many-arguments
                  for_each_handler=None, map_handler=None, object_to_id=None, stream_version=None):
    full_replication = CONFIG.get("full_replication", False)
    schema = load_schema(schema_name)
    bookmark_property = 'updated_at'

    singer.write_schema(schema_name,
                        schema,
                        ["id"],
                        bookmark_properties=[bookmark_property])

    start = get_start(schema_name)
    start_dt = pendulum.parse(start)
    updated_since = start_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    with Transformer() as transformer:
        page = 1
        while page is not None:
            url = get_url(endpoint or schema_name)
            params = {"updated_since": updated_since} if with_updated_since else {}
            params['page'] = page
            response = request(url, params)
            path = path or schema_name
            data = response[path]
            time_extracted = utils.now()

            for row in data:
                if map_handler is not None:
                    row = map_handler(row)

                if object_to_id is not None:
                    for key in object_to_id:
                        if row[key] is not None:
                            row[key + '_id'] = row[key]['id']
                        else:
                            row[key + '_id'] = None

                remove_empty_date_times(row, schema)

                item = transformer.transform(row, schema)

                append_times_to_dates(item, date_fields)

                if item[bookmark_property] >= start:
                    new_record = singer.RecordMessage(
                        stream=schema_name,
                        record=item,
                        version=stream_version,
                        time_extracted=time_extracted)
                    singer.write_message(new_record)

                    # take any additional actions required for the currently loaded endpoint
                    if for_each_handler is not None:
                        for_each_handler(row, time_extracted=time_extracted)

                    utils.update_state(STATE, schema_name, item[bookmark_property])
            page = response['next_page']

    singer.write_state(STATE)


def sync_time_entries(stream_version=None):
    def for_each_time_entry(time_entry, time_extracted):
        # Extract external_reference
        external_reference_schema = load_and_write_schema("external_reference")
        load_and_write_schema("time_entry_external_reference",
                              key_properties=["time_entry_id", "external_reference_id"])
        if time_entry['external_reference'] is not None:
            with Transformer() as transformer:
                external_reference = time_entry['external_reference']
                external_reference = transformer.transform(external_reference,
                                                           external_reference_schema)

                new_record = singer.RecordMessage(
                    stream="external_reference",
                    record=external_reference,
                    version=stream_version,
                    time_extracted=time_extracted)
                singer.write_message(new_record)

                # Create pivot row for time_entry and external_reference
                pivot_row = {
                    'time_entry_id': time_entry['id'],
                    'external_reference_id': external_reference['id']
                }

                new_record = singer.RecordMessage(
                    stream="time_entry_external_reference",
                    record=pivot_row,
                    version=stream_version,
                    time_extracted=time_extracted)
                singer.write_message(new_record)

    sync_endpoint("time_entries", for_each_handler=for_each_time_entry,
                  object_to_id=[
                      'user',
                      'user_assignment',
                      'client',
                      'project',
                      'task',
                      'task_assignment',
                      'external_reference',
                      'invoice'
                  ], stream_version=stream_version)


def sync_invoices(stream_version=None):
    def for_each_invoice(invoice, time_extracted):
        def map_invoice_message(message):
            message['invoice_id'] = invoice['id']
            return message

        def map_invoice_payment(payment):
            payment['invoice_id'] = invoice['id']
            payment['payment_gateway_id'] = payment['payment_gateway']['id']
            payment['payment_gateway_name'] = payment['payment_gateway']['name']
            return payment

        # Sync invoice messages
        sync_endpoint("invoice_messages",
                      endpoint=("invoices/{}/messages".format(invoice['id'])),
                      path="invoice_messages",
                      with_updated_since=False,
                      map_handler=map_invoice_message,
                      stream_version=stream_version)

        # Sync invoice payments
        sync_endpoint("invoice_payments",
                      endpoint=("invoices/{}/payments".format(invoice['id'])),
                      path="invoice_payments",
                      with_updated_since=False,
                      map_handler=map_invoice_payment,
                      date_fields=["send_reminder_on"],
                      stream_version=stream_version)

        # Extract all invoice_line_items
        line_items_schema = load_and_write_schema("invoice_line_items")
        with Transformer() as transformer:
            for line_item in invoice['line_items']:
                line_item['invoice_id'] = invoice['id']
                if line_item['project'] is not None:
                    line_item['project_id'] = line_item['project']['id']
                else:
                    line_item['project_id'] = None
                line_item = transformer.transform(line_item, line_items_schema)

                new_record = singer.RecordMessage(
                    stream="invoice_line_items",
                    record=line_item,
                    version=stream_version,
                    time_extracted=time_extracted)
                singer.write_message(new_record)

    sync_endpoint("invoices", for_each_handler=for_each_invoice,
                  object_to_id=['client', 'estimate', 'retainer', 'creator'],
                  stream_version=stream_version)


def sync_estimates(stream_version=None):
    def map_estimate_message(message):
        message['estimate_id'] = message['id']
        return message

    def for_each_estimate(estimate, time_extracted):
        # Sync estimate messages
        sync_endpoint("estimate_messages",
                      endpoint=("estimates/{}/messages".format(estimate['id'])),
                      path="estimate_messages",
                      with_updated_since=False,
                      date_fields=["send_reminder_on"],
                      map_handler=map_estimate_message,
                      stream_version=stream_version)

        # Extract all estimate_line_items
        line_items_schema = load_and_write_schema("estimate_line_items")
        with Transformer() as transformer:
            for line_item in estimate['line_items']:
                line_item['estimate_id'] = estimate['id']
                line_item = transformer.transform(line_item, line_items_schema)

                new_record = singer.RecordMessage(
                    stream="estimate_line_items",
                    record=line_item,
                    version=stream_version,
                    time_extracted=time_extracted)
                singer.write_message(new_record)


    sync_endpoint("estimates",
                  for_each_handler=for_each_estimate,
                  date_fields=["issue_date"],
                  object_to_id=['client', 'creator'],
                  stream_version=stream_version)


def sync_roles(stream_version=None):
    def for_each_role(role, time_extracted):
        # Extract user_roles
        load_and_write_schema("user_roles", key_properties=["user_id", "role_id"])
        for user_id in role['user_ids']:
            pivot_row = {
                'role_id': role['id'],
                'user_id': user_id
            }

            new_record = singer.RecordMessage(
                stream="user_roles",
                record=pivot_row,
                version=stream_version,
                time_extracted=time_extracted)
            singer.write_message(new_record)

    sync_endpoint("roles", for_each_handler=for_each_role, stream_version=stream_version)


def sync_users(stream_version=None):
    def for_each_user(user, time_extracted): #pylint: disable=unused-argument
        def map_user_projects(project_assignment):
            project_assignment['user'] = user
            return project_assignment

        def for_each_user_project(user_project_assignment, time_extracted):
            # Extract user_project_tasks
            load_and_write_schema("user_project_tasks",
                                  key_properties=["user_id", "project_task_id"])
            for project_task in user_project_assignment['task_assignments']:
                pivot_row = {
                    'user_id': user['id'],
                    'project_task_id': project_task['id']
                }

                new_record = singer.RecordMessage(
                    stream="user_project_tasks",
                    record=pivot_row,
                    version=stream_version,
                    time_extracted=time_extracted)
                singer.write_message(new_record)

        sync_endpoint("user_projects",
                      endpoint=("users/{}/project_assignments".format(user['id'])),
                      path="project_assignments",
                      with_updated_since=False,
                      object_to_id=['project', 'client', 'user'],
                      map_handler=map_user_projects,
                      for_each_handler=for_each_user_project,
                      stream_version=stream_version)

    sync_endpoint("users", for_each_handler=for_each_user, stream_version=stream_version)


def sync_expenses(stream_version=None):
    def map_expense(expense):
        if expense['receipt'] is None:
            expense['receipt_url'] = None
            expense['receipt_file_name'] = None
            expense['receipt_file_size'] = None
            expense['receipt_content_type'] = None
        else:
            expense['receipt_url'] = expense['receipt']['url']
            expense['receipt_file_name'] = expense['receipt']['file_name']
            expense['receipt_file_size'] = expense['receipt']['file_size']
            expense['receipt_content_type'] = expense['receipt']['content_type']
        return expense

    sync_endpoint("expenses",
                  map_handler=map_expense,
                  object_to_id=[
                      'client',
                      'project',
                      'expense_category',
                      'user',
                      'user_assignment',
                      'invoice'
                  ],
                  stream_version=stream_version)


def do_sync():
    LOGGER.info("Starting sync")

    stream_version = get_stream_version()
    full_replication = CONFIG.get("full_replication", False)

    company = get_company()

    # Grab all clients and client contacts. Contacts have client FKs so grab
    # them last.
    sync_endpoint("clients", stream_version=stream_version)
    sync_endpoint("contacts", object_to_id=['client'], stream_version=stream_version)
    sync_roles(stream_version=stream_version)

    # Sync related project objects
    sync_endpoint("projects", object_to_id=['client'], stream_version=stream_version)
    sync_endpoint("tasks", stream_version=stream_version)
    sync_endpoint("project_tasks", endpoint='task_assignments', path='task_assignments',
                  object_to_id=['project', 'task'], stream_version=stream_version)
    sync_endpoint("project_users", endpoint='user_assignments', path='user_assignments',
                  object_to_id=['project', 'user'], stream_version=stream_version)

    # Sync users
    sync_users(stream_version=stream_version)

    if company['expense_feature']:
        # Sync expenses and their categories
        sync_endpoint("expense_categories", stream_version=stream_version)
        sync_expenses(stream_version=stream_version)
    else:
        LOGGER.info("Expense Feature not enabled, skipping.")

    if company['invoice_feature']:
        # Sync invoices and all related records
        sync_endpoint("invoice_item_categories", stream_version=stream_version)
        sync_invoices(stream_version=stream_version)
    else:
        LOGGER.info("Invoice Feature not enabled, skipping.")

    if company['estimate_feature']:
        # Sync estimates and all related records
        sync_endpoint("estimate_item_categories", stream_version=stream_version)
        sync_estimates(stream_version=stream_version)
    else:
        LOGGER.info("Estimate Feature not enabled, skipping.")

    # Sync Time Entries along with their external reference objects
    sync_time_entries(stream_version=stream_version)
    
    if full_replication:
        for stream in ALL_STREAMS:
            singer.write_version(stream, stream_version)

    LOGGER.info("Sync complete")

def do_discover():
    print('{"streams":[]}')

def main_impl():
    args = utils.parse_args(REQUIRED_CONFIG_KEYS)
    CONFIG.update(args.config)
    global AUTH  # pylint: disable=global-statement
    AUTH = Auth(CONFIG['client_id'], CONFIG['client_secret'], CONFIG['refresh_token'])
    STATE.update(args.state)
    if args.discover:
        do_discover()
    else:
        do_sync()

def main():
    try:
        main_impl()
    except Exception as exc:
        LOGGER.critical(exc)
        raise exc


if __name__ == "__main__":
    main()
