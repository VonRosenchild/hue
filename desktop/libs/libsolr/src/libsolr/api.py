#!/usr/bin/env python
# -- coding: utf-8 --
# Licensed to Cloudera, Inc. under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  Cloudera, Inc. licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import json
import logging
import urllib

from itertools import groupby

from django.utils.translation import ugettext as _

from desktop.lib.exceptions_renderable import PopupException
from desktop.lib.i18n import smart_str
from desktop.lib.rest.http_client import HttpClient, RestException
from desktop.lib.rest import resource

from search.conf import EMPTY_QUERY, SECURITY_ENABLED


LOG = logging.getLogger(__name__)

DEFAULT_USER = 'hue'


def utf_quoter(what):
  return urllib.quote(unicode(what).encode('utf-8'), safe='~@#$&()*!+=;,.?/\'')


class SolrApi(object):
  """
  http://wiki.apache.org/solr/CoreAdmin#CoreAdminHandler
  """
  def __init__(self, solr_url, user, security_enabled=SECURITY_ENABLED.get()):
    self._url = solr_url
    self._user = user
    self._client = HttpClient(self._url, logger=LOG)
    self.security_enabled = security_enabled
    if self.security_enabled:
      self._client.set_kerberos_auth()
    self._root = resource.Resource(self._client)


  def _get_params(self):
    if self.security_enabled:
      return (('doAs', self._user ),)
    return (('user.name', DEFAULT_USER), ('doAs', self._user),)


  def _get_q(self, query):
    q_template = '(%s)' if len(query['qs']) >= 2 else '%s'
    return 'OR'.join([q_template % (q['q'] or EMPTY_QUERY.get()) for q in query['qs']]).encode('utf-8')

  def _get_aggregate_function(self, facet):
    props = {
        'field': facet['field'],
        'aggregate': facet['properties']['aggregate'] if 'properties' in facet else facet['aggregate']
    }
    if props['aggregate'] == 'median':
      return 'percentile(%(field)s,50)' % props
    else:
      return '%(aggregate)s(%(field)s)' % props

  def _get_range_borders(self, collection, query):
    props = {}
    GAPS = {
        '5MINUTES': {
            'histogram-widget': {'coeff': '+3', 'unit': 'SECONDS'}, # 100 slots
            'facet-widget': {'coeff': '+30', 'unit': 'SECONDS'}, # 10 slots
        },
        '15MINUTES': {
            'histogram-widget': {'coeff': '+10', 'unit': 'SECONDS'},
            'facet-widget': {'coeff': '+90', 'unit': 'SECONDS'},
        },
        '1HOURS': {
            'histogram-widget': {'coeff': '+36', 'unit': 'SECONDS'},
            'facet-widget': {'coeff': '+6', 'unit': 'MINUTES'},
        },
        '1DAYS': {
            'histogram-widget': {'coeff': '+15', 'unit': 'MINUTES'},
            'facet-widget': {'coeff': '+144', 'unit': 'MINUTES'},
        },
        '2DAYS': {
            'histogram-widget': {'coeff': '+30', 'unit': 'MINUTES'},
            'facet-widget': {'coeff': '+288', 'unit': 'MINUTES'},
        },
        '7DAYS': {
            'histogram-widget': {'coeff': '+3', 'unit': 'HOURS'},
            'facet-widget': {'coeff': '+1008', 'unit': 'MINUTES'},
        }
        # ...
    }
    fq_fields = [fq['field'] for fq in query['fqs']]
    time_field = collection['timeFilter'].get('field')

    # fqs overrides main time filter
    if time_field and time_field not in fq_fields or True:
      if collection['timeFilter']['type'] == 'rolling' and collection['timeFilter']['value'] != 'all': # todo all for chart range? guess based on min
        props['field'] = collection['timeFilter']['field']
        props['from'] = 'NOW-%s' % collection['timeFilter']['value']
        props['to'] = 'NOW'
        props['gap'] = GAPS.get(collection['timeFilter']['value'])
      elif collection['timeFilter']['type'] == 'fixed' and collection['timeFilter']['from'] and collection['timeFilter']['to']:
        props['field'] = collection['timeFilter']['field']
        props['from'] = collection['timeFilter']['from']
        props['to'] = collection['timeFilter']['to']
            
    return props

  def _get_fq(self, collection, query):
    params = ()

    timeFilter = self._get_range_borders(collection, query)
    if timeFilter:
      params += (('fq', urllib.unquote(utf_quoter('%(field)s:[%(from)s TO %(to)s}' % timeFilter))),)

    # Merge facets queries on same fields
    grouped_fqs = groupby(query['fqs'], lambda x: (x['type'], x['field']))
    merged_fqs = []
    for key, group in grouped_fqs:
      field_fq = next(group)
      for fq in group:
        for f in fq['filter']:
          field_fq['filter'].append(f)
      merged_fqs.append(field_fq)

    for fq in merged_fqs:
      if fq['type'] == 'field':
        fields = fq['field'] if type(fq['field']) == list else [fq['field']] # 2D facets support
        for field in fields:
          f = []
          for _filter in fq['filter']:
            values = _filter['value'] if type(_filter['value']) == list else [_filter['value']] # 2D facets support
            if fields.index(field) < len(values): # Lowest common field denominator
              value = values[fields.index(field)]
              exclude = '-' if _filter['exclude'] else ''
              if value is not None and ' ' in smart_str(value):
                f.append('%s%s:"%s"' % (exclude, field, value))
              else:
                f.append('%s{!field f=%s}%s' % (exclude, field, value))
          _params ='{!tag=%(id)s}' % fq + ' '.join(f)
          params += (('fq', urllib.unquote(utf_quoter(_params))),)
      elif fq['type'] == 'range':
        params += (('fq', '{!tag=%(id)s}' % fq + ' '.join([urllib.unquote(
                    utf_quoter('%s%s:[%s TO %s}' % ('-' if field['exclude'] else '', fq['field'], f['from'], f['to']))) for field, f in zip(fq['filter'], fq['properties'])])),)
      elif fq['type'] == 'range-up':
        params += (('fq', '{!tag=%(id)s}' % fq + ' '.join([urllib.unquote(
                    utf_quoter('%s%s:[%s TO %s}' % ('-' if field['exclude'] else '', fq['field'], f['from'] if fq['is_up'] else '*', '*' if fq['is_up'] else f['from'])))
                                                          for field, f in zip(fq['filter'], fq['properties'])])),)
    return params

  def query(self, collection, query):
    solr_query = {}

    solr_query['collection'] = collection['name']

    if query.get('download'):
      solr_query['rows'] = 1000
      solr_query['start'] = 0
    else:
      solr_query['rows'] = int(collection['template']['rows'] or 10)
      solr_query['start'] = int(query['start'])

    solr_query['rows'] = min(solr_query['rows'], 1000)
    solr_query['start'] = min(solr_query['start'], 10000)

    params = self._get_params() + (
        ('q', self._get_q(query)),
        ('wt', 'json'),
        ('rows', solr_query['rows']),
        ('start', solr_query['start']),
    )

    if any(collection['facets']):
      params += (
        ('facet', 'true'),
        ('facet.mincount', 0),
        ('facet.limit', 10),
      )
      json_facets = {}

      timeFilter = self._get_range_borders(collection, query)

      for facet in collection['facets']:
        if facet['type'] == 'query':
          params += (('facet.query', '%s' % facet['field']),)
        elif facet['type'] == 'range' or facet['type'] == 'range-up':
          keys = {
              'id': '%(id)s' % facet,
              'field': facet['field'],
              'key': '%(field)s-%(id)s' % facet,
              'start': facet['properties']['start'],
              'end': facet['properties']['end'],
              'gap': facet['properties']['gap'],              
              'mincount': int(facet['properties']['mincount'])
          }

          if timeFilter:
#            if facet['widgetType'] == 'histogram-widget':
              gap = timeFilter['gap'][facet['widgetType']]
              keys.update({
                'start': '%(from)s/%(unit)s' % {'from': timeFilter['from'], 'unit': gap['unit']},
                'end': '%(to)s/%(unit)s' % {'to': timeFilter['to'], 'unit': gap['unit']},
                'gap': '%(coeff)s%(unit)s/%(unit)s' % gap, # add a 'auto'
              })
              print keys

          params += (
             ('facet.range', '{!key=%(key)s ex=%(id)s f.%(field)s.facet.range.start=%(start)s f.%(field)s.facet.range.end=%(end)s f.%(field)s.facet.range.gap=%(gap)s f.%(field)s.facet.mincount=%(mincount)s}%(field)s' % keys),
          )
        elif facet['type'] == 'field':
          keys = {
              'id': '%(id)s' % facet,
              'field': facet['field'],
              'key': '%(field)s-%(id)s' % facet,
              'limit': int(facet['properties'].get('limit', 10)) + (1 if facet['widgetType'] == 'facet-widget' else 0),
              'mincount': int(facet['properties']['mincount'])
          }
          params += (
              ('facet.field', '{!key=%(key)s ex=%(id)s f.%(field)s.facet.limit=%(limit)s f.%(field)s.facet.mincount=%(mincount)s}%(field)s' % keys),
          )
        elif facet['type'] == 'nested':
          _f = {
              'field': facet['field'],
              'limit': int(facet['properties'].get('limit', 10)) + (1 if facet['widgetType'] == 'facet-widget' else 0),
              'mincount': int(facet['properties']['mincount'])
          }

          if 'start' in facet['properties']:
            _f.update({
                'type': 'range',
                'start': facet['properties']['start'],
                'end': facet['properties']['end'],
                'gap': facet['properties']['gap'],
            })
          else:
            _f.update({
                'type': 'terms',
                'field': facet['field'],
                'excludeTags': facet['id']
            })

          if facet['properties']['facets']:
            if facet['properties']['facets'][0]['aggregate'] == 'count':
              _f['facet'] = {
                  'd2': {
                      'type': 'terms',
                      'field': '%(field)s' % facet['properties']['facets'][0]
                  }
              }
              if len(facet['properties']['facets']) > 1: # Get 3rd dimension calculation
                _f['facet']['d2']['facet'] = {
                    'd2': self._get_aggregate_function(facet['properties']['facets'][1])
                }
            else:
              _f['facet'] = {
                  'd2': self._get_aggregate_function(facet['properties']['facets'][0])
              }

          json_facets[facet['id']] = _f
        elif facet['type'] == 'function':
          json_facets[facet['id']] = self._get_aggregate_function(facet)
        elif facet['type'] == 'pivot':
          if facet['properties']['facets'] or facet['widgetType'] == 'map-widget':
            fields = facet['field']
            fields_limits = []
            for f in facet['properties']['facets']:
              fields_limits.append('f.%s.facet.limit=%s' % (f['field'], f['limit']))
              fields_limits.append('f.%s.facet.mincount=%s' % (f['field'], f['mincount']))
              fields += ',' + f['field']
            keys = {
                'id': '%(id)s' % facet,
                'key': '%(field)s-%(id)s' % facet,
                'field': facet['field'],
                'fields': fields,
                'limit': int(facet['properties'].get('limit', 10)),
                'mincount': int(facet['properties']['mincount']),
                'fields_limits': ' '.join(fields_limits)
            }
            params += (
                ('facet.pivot', '{!key=%(key)s ex=%(id)s f.%(field)s.facet.limit=%(limit)s f.%(field)s.facet.mincount=%(mincount)s %(fields_limits)s}%(fields)s' % keys),
            )

      if json_facets:
        params += (
            ('json.facet', json.dumps(json_facets)),
        )

    params += self._get_fq(collection, query)

    if collection['template']['fieldsSelected'] and collection['template']['isGridLayout']:
      fields = set(collection['template']['fieldsSelected'] + [collection['idField']] if collection['idField'] else [])
      # Add field if needed
      if collection['template']['leafletmap'].get('latitudeField'):
        fields.add(collection['template']['leafletmap']['latitudeField'])
      if collection['template']['leafletmap'].get('longitudeField'):
        fields.add(collection['template']['leafletmap']['longitudeField'])
      if collection['template']['leafletmap'].get('labelField'):
        fields.add(collection['template']['leafletmap']['labelField'])
      params += (('fl', urllib.unquote(utf_quoter(','.join(list(fields))))),)
    else:
      params += (('fl', '*'),)

    params += (
      ('hl', 'true'),
      ('hl.fl', '*'),
      ('hl.snippets', 3),
      ('hl.fragsize', 0),
    )

    if collection['template']['fieldsSelected']:
      fields = []
      for field in collection['template']['fieldsSelected']:
        attribute_field = filter(lambda attribute: field == attribute['name'], collection['template']['fieldsAttributes'])
        if attribute_field:
          if attribute_field[0]['sort']['direction']:
            fields.append('%s %s' % (field, attribute_field[0]['sort']['direction']))
      if fields:
        params += (
          ('sort', ','.join(fields)),
        )

    response = self._root.get('%(collection)s/select' % solr_query, params)
    return self._get_json(response)


  def suggest(self, solr_query, hue_core):
    try:
      params = self._get_params() + (
          ('q', solr_query['q']),
          ('wt', 'json'),
      )
      response = self._root.get('%(collection)s/suggest' % solr_query, params)
      return self._get_json(response)
    except RestException, e:
      raise PopupException(e, title=_('Error while accessing Solr'))


  def collections(self):
    try:
      params = self._get_params() + (
          ('detail', 'true'),
          ('path', '/clusterstate.json'),
      )
      response = self._root.get('zookeeper', params=params)
      return json.loads(response['znode'].get('data', '{}'))
    except RestException, e:
      raise PopupException(e, title=_('Error while accessing Solr'))


  def aliases(self):
    try:
      params = self._get_params() + (
          ('detail', 'true'),
          ('path', '/aliases.json'),
      )
      response = self._root.get('zookeeper', params=params)
      return json.loads(response['znode'].get('data', '{}')).get('collection', {})
    except RestException, e:
      raise PopupException(e, title=_('Error while accessing Solr'))


  def collection_or_core(self, hue_collection):
    if hue_collection.is_core_only:
      return self.core(hue_collection.name)
    else:
      return self.collection(hue_collection.name)


  def collection(self, name):
    try:
      collections = self.collections()
      return collections[name]
    except Exception, e:
      raise PopupException(e, title=_('Error while accessing Solr'))

  def create_collection(self, name, shards=1, replication=1):
    try:
      params = self._get_params() + (
        ('action', 'CREATE'),
        ('name', name),
        ('numShards', shards),
        ('replicationFactor', replication),
        ('collection.configName', name),
        ('wt', 'json')
      )

      response = self._root.post('admin/collections', params=params, contenttype='application/json')
      if 'success' in response:
        return True
      else:
        LOG.error("Could not create collection. Check response:\n%s" % json.dumps(response, indent=2))
        return False
    except RestException, e:
      raise PopupException(e, title=_('Error while accessing Solr'))

  def create_core(self, name, instance_dir, shards=1, replication=1):
    try:
      params = self._get_params() + (
        ('action', 'CREATE'),
        ('name', name),
        ('instanceDir', instance_dir),
        ('wt', 'json'),
      )

      response = self._root.post('admin/cores', params=params, contenttype='application/json')
      if response.get('responseHeader',{}).get('status',-1) == 0:
        return True
      else:
        LOG.error("Could not create core. Check response:\n%s" % json.dumps(response, indent=2))
        return False
    except RestException, e:
      if 'already exists' in e.message:
        LOG.warn("Could not create collection.", exc_info=True)
        return False
      else:
        raise PopupException(e, title=_('Error while accessing Solr'))

  def remove_collection(self, name, replication=1):
    try:
      params = self._get_params() + (
        ('action', 'DELETE'),
        ('name', name),
        ('replicationFactor', replication),
        ('wt', 'json')
      )

      response = self._root.post('admin/collections', params=params, contenttype='application/json')
      if 'success' in response:
        return True
      else:
        LOG.error("Could not remove collection. Check response:\n%s" % json.dumps(response, indent=2))
        return False
    except RestException, e:
      raise PopupException(e, title=_('Error while accessing Solr'))

  def remove_core(self, name):
    try:
      params = self._get_params() + (
        ('action', 'UNLOAD'),
        ('name', name),
        ('deleteIndex', 'true'),
        ('wt', 'json')
      )

      response = self._root.post('admin/cores', params=params, contenttype='application/json')
      if 'success' in response:
        return True
      else:
        LOG.error("Could not remove core. Check response:\n%s" % json.dumps(response, indent=2))
        return False
    except RestException, e:
      raise PopupException(e, title=_('Error while accessing Solr'))

  def add_fields(self, collection, fields):
    try:
      params = self._get_params()
      return self._root.post('%s/schema/fields' % collection, params=params, data=json.dumps(fields), contenttype='application/json')
    except RestException, e:
      raise PopupException(e, title=_('Error while accessing Solr'))

  def cores(self):
    try:
      params = self._get_params() + (
          ('wt', 'json'),
      )
      return self._root.get('admin/cores', params=params)['status']
    except RestException, e:
      raise PopupException(e, title=_('Error while accessing Solr'))

  def core(self, core):
    try:
      params = self._get_params() + (
          ('wt', 'json'),
          ('core', core),
      )
      return self._root.get('admin/cores', params=params)
    except RestException, e:
      raise PopupException(e, title=_('Error while accessing Solr'))

  def schema(self, core):
    try:
      params = self._get_params() + (
          ('wt', 'json'),
          ('file', 'schema.xml'),
      )
      return self._root.get('%(core)s/admin/file' % {'core': core}, params=params)
    except RestException, e:
      raise PopupException(e, title=_('Error while accessing Solr'))

  def fields(self, core, dynamic=False):
    try:
      params = self._get_params() + (
          ('wt', 'json'),
          ('fl', '*'),
      )
      if not dynamic:
        params += (('show', 'schema'),)
      response = self._root.get('%(core)s/admin/luke' % {'core': core}, params=params)
      return self._get_json(response)
    except RestException, e:
      raise PopupException(e, title=_('Error while accessing Solr'))

  def luke(self, core):
    try:
      params = self._get_params() + (
          ('wt', 'json'),
      )
      response = self._root.get('%(core)s/admin/luke' % {'core': core}, params=params)
      return self._get_json(response)
    except RestException, e:
      raise PopupException(e, title=_('Error while accessing Solr'))

  def schema_fields(self, core):
    try:
      params = self._get_params() + (
          ('wt', 'json'),
      )
      response = self._root.get('%(core)s/schema/fields' % {'core': core}, params=params)
      return self._get_json(response)
    except RestException, e:
      raise PopupException(e, title=_('Error while accessing Solr'))

  def stats(self, core, fields, query=None, facet=''):
    try:
      params = self._get_params() + (
          ('q', self._get_q(query) if query is not None else EMPTY_QUERY.get()),
          ('wt', 'json'),
          ('rows', 0),
          ('stats', 'true'),
      )

      if query is not None:
        params += self._get_fq(query)

      if facet:
        params += (('stats.facet', facet),)

      params += tuple([('stats.field', field) for field in fields])
      response = self._root.get('%(core)s/select' % {'core': core}, params=params)
      return self._get_json(response)
    except RestException, e:
      raise PopupException(e, title=_('Error while accessing Solr'))

  def terms(self, core, field, properties=None):
    try:
      params = self._get_params() + (
          ('wt', 'json'),
          ('rows', 0),
          ('terms.fl', field),
      )
      if properties:
        for key, val in properties.iteritems():
          params += ((key, val),)

      response = self._root.get('%(core)s/terms' % {'core': core}, params=params)
      return self._get_json(response)
    except RestException, e:
      raise PopupException(e, title=_('Error while accessing Solr'))

  def get(self, core, doc_id):
    try:
      params = self._get_params() + (
          ('id', doc_id),
          ('wt', 'json'),
      )
      response = self._root.get('%(core)s/get' % {'core': core}, params=params)
      return self._get_json(response)
    except RestException, e:
      raise PopupException(e, title=_('Error while accessing Solr'))

  @classmethod
  def _get_json(cls, response):
    if type(response) != dict:
      # Got 'plain/text' mimetype instead of 'application/json'
      try:
        response = json.loads(response)
      except ValueError, e:
        # Got some null bytes in the response
        LOG.error('%s: %s' % (unicode(e), repr(response)))
        response = json.loads(response.replace('\x00', ''))
    return response

  def uniquekey(self, collection):
    try:
      params = self._get_params() + (
          ('wt', 'json'),
      )
      response = self._root.get('%s/schema/uniquekey' % collection, params=params)
      return self._get_json(response)['uniqueKey']
    except RestException, e:
      raise PopupException(e, title=_('Error while accessing Solr'))

  def update(self, collection_or_core_name, data, content_type='csv'):
    try:
      if content_type == 'csv':
        params = self._get_params() + (
          ('wt', 'json'),
          ('overwrite', 'true'),
        )
        content_type = 'application/csv'
      elif content_type == 'json':
        params = self._get_params() + (
          ('wt', 'json'),
          ('overwrite', 'true'),
        )
        content_type = 'application/json'
      else:
        LOG.error("Could not update index for %s. Unsupported content type %s. Allowed content types: csv" % (collection_or_core_name, content_type))
        return False

      self._root.post('%s/update' % collection_or_core_name, contenttype=content_type, params=params, data=data)
      return True
    except RestException, e:
      raise PopupException(e, title=_('Error while accessing Solr'))
