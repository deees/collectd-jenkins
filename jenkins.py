#!/usr/bin/env python
# Copyright (C) 2017 SignalFx, Inc.

import collections
import json
import pprint
import time
try:
    from urllib import quote as urllib_quote
except ImportError:
    from urllib.parse import quote as urllib_quote
import urllib2
import urllib_auth_n_ssl_handler
import urlparse

import collectd

PLUGIN_NAME = 'jenkins'
DEFAULT_API_TIMEOUT = 60


Metric = collections.namedtuple('Metric', ('name', 'type'))

JOB_METRICS = {
    'duration':
        Metric('jenkins.job.duration', 'gauge'),
}

NODE_METRICS = {
    'vm.memory.total.used':
        Metric('jenkins.node.vm.memory.total.used', 'gauge'),
    'vm.memory.heap.usage':
        Metric('jenkins.node.vm.memory.heap.usage', 'gauge'),
    'vm.memory.non-heap.used':
        Metric('jenkins.node.vm.memory.non-heap.used', 'gauge'),
    'jenkins.queue.size.value':
        Metric('jenkins.node.queue.size.value', 'gauge'),
    'jenkins.health-check.score':
        Metric('jenkins.node.health-check.score', 'gauge'),
    'jenkins.executor.count.value':
        Metric('jenkins.node.executor.count.value', 'gauge'),
    'jenkins.executor.in-use.value':
        Metric('jenkins.node.executor.in-use.value', 'gauge')
}


HEALTH_METRICS = {
    'disk-space':
        Metric('jenkins.node.health.disk.space', 'gauge'),
    'temporary-space':
        Metric('jenkins.node.health.temporary.space', 'gauge'),
    'plugins':
        Metric('jenkins.node.health.plugins', 'gauge'),
    'thread-deadlock':
        Metric('jenkins.node.health.thread-deadlock', 'gauge')
}

NODE_STATUS_METRICS = {
    'ping':
        Metric('jenkins.node.online.status', 'gauge')
}

COMPUTER_STATUS_METRICS = {
    'offline':
        Metric('jenkins.node.computer.online.status', 'gauge')
}


def get_ssl_params(data):
    '''
    Helper method to prepare auth tuple
    '''
    key_file = None
    cert_file = None
    ca_certs = None

    ssl_keys = data['ssl_keys']
    if 'ssl_certificate' in ssl_keys and 'ssl_keyfile' in ssl_keys:
        key_file = ssl_keys['ssl_keyfile']
        cert_file = ssl_keys['ssl_certificate']

    if 'ssl_ca_certs' in ssl_keys:
        ca_certs = ssl_keys['ssl_ca_certs']

    return (key_file, cert_file, ca_certs)


def load_json(resp, url):
    try:
        return json.load(resp)
    except ValueError, e:
        collectd.error("Error parsing JSON for API call (%s) %s" % (e, url))
        return None


def _api_call(url, type, opener, http_timeout):
    """
    Makes a REST call against the Jenkins API.
    Args:
    url (str): The URL to get, including endpoint
    Returns:
    list: The JSON response
    """
    parsed_url = urlparse.urlparse(url)
    url = '{0}://{1}{2}'.format(parsed_url.scheme, parsed_url.netloc, urllib_quote(parsed_url.path))
    resp = None
    try:
        urllib2.install_opener(opener)
        resp = urllib2.urlopen(url, timeout=http_timeout)
        return load_json(resp, url)
    except urllib2.HTTPError as e:
        if e.code == 500 and type == "healthcheck":
            return load_json(e, url)
        else:
            collectd.error("Error making API call (%s) %s" % (e, url))
            return None
    except urllib2.URLError as e:
        collectd.error("Error making API call (%s) %s" % (e, url))
        return None
    finally:
        if resp:
            resp.close()


def ping_check(url, opener, http_timeout):
    """
    Makes a REST call against Jenkins to get alive status.
    Args:
    url (str): The URL to get, including endpoint
    Returns:
    bool: The Success or Failure status based on HTTP response
    """
    resp = None
    try:
        urllib2.install_opener(opener)
        resp = urllib2.urlopen(url, timeout=http_timeout)
        val = resp.read().strip()
        if val == "pong":
            return True
        else:
            return False
    except (urllib2.HTTPError, urllib2.URLError) as e:
        collectd.error("Error making API call (%s) %s" % (e, url))
        return False
    finally:
        if resp:
            resp.close()


def get_auth_handler(module_config):

    key_file, cert_file, ca_certs = get_ssl_params(module_config)

    if key_file is not None and cert_file is not None:
        auth_handler = urllib_auth_n_ssl_handler.HTTPSHandler(user=module_config['username'],
                                                              passwd=module_config['api_token'],
                                                              key_file=key_file,
                                                              cert_file=cert_file,
                                                              ca_certs=ca_certs)
    else:
        auth_handler = urllib_auth_n_ssl_handler.HTTPBasicPriorAuthHandler()
        auth_handler.add_password(realm=None,
                                  uri=module_config['base_url'],
                                  user=module_config['username'],
                                  passwd=module_config['api_token'])

    return auth_handler


def read_config(conf):
    '''
    Reads the configurations provided by the user
    '''
    module_config = {
        'member_id': None,
        'plugin_config': {},
        'username': None,
        'api_token': None,
        'opener': None,
        'metrics_key': None,
        'custom_dimensions': {},
        'enhanced_metrics': False,
        'include_optional_metrics': set(),
        'exclude_optional_metrics': set(),
        'computer_metrics': False,
        'job_metrics': False,
        'http_timeout': DEFAULT_API_TIMEOUT,
        'jobs_last_timestamp': {},
        'ssl_keys': {}
    }

    interval = None
    testing = False

    required_keys = ('Host', 'Port')
    auth_keys = ('Username', 'APIToken', 'MetricsKey')

    for val in conf.children:
        if val.key in required_keys:
            module_config['plugin_config'][val.key] = val.values[0]
        elif val.key == 'Interval' and val.values[0]:
            interval = val.values[0]
        elif val.key in auth_keys and val.key == 'Username' and \
                val.values[0]:
            module_config['username'] = val.values[0]
        elif val.key in auth_keys and val.key == 'APIToken' and \
                val.values[0]:
            module_config['api_token'] = val.values[0]
        elif val.key in auth_keys and val.key == 'MetricsKey' and \
                val.values[0]:
            module_config['metrics_key'] = val.values[0]
        elif val.key == 'Dimension':
            if len(val.values) == 2:
                module_config['custom_dimensions'].update({val.values[0]: val.values[1]})
            else:
                collectd.warning("WARNING: Dimension Key Value format required")
        elif val.key == 'EnhancedMetrics' and val.values[0]:
            module_config['enhanced_metrics'] = str_to_bool(val.values[0])
        elif val.key == 'IncludeMetric' and val.values[0] and val.values[0] not in NODE_METRICS:
            module_config['include_optional_metrics'].add(val.values[0])
        elif val.key == 'ExcludeMetric' and val.values[0] and val.values[0] not in NODE_METRICS:
            module_config['exclude_optional_metrics'].add(val.values[0])
        elif val.key == 'ComputerMetrics' and val.values[0]:
            module_config['computer_metrics'] = str_to_bool(val.values[0])
        elif val.key == 'JobMetrics' and val.values[0]:
            module_config['job_metrics'] = str_to_bool(val.values[0])
        elif val.key == 'ssl_keyfile' and val.values[0]:
            module_config['ssl_keys']['ssl_keyfile'] = val.values[0]
        elif val.key == 'ssl_certificate' and val.values[0]:
            module_config['ssl_keys']['ssl_certificate'] = val.values[0]
        elif val.key == 'ssl_ca_certs' and val.values[0]:
            module_config['ssl_keys']['ssl_ca_certs'] = val.values[0]
        elif val.key == 'Testing' and str_to_bool(val.values[0]):
            testing = True

    # Make sure all required config settings are present, and log them
    collectd.info("Using config settings:")
    for key in required_keys:
        val = module_config['plugin_config'].get(key)
        if val is None:
            raise ValueError("Missing required config setting: %s" % key)
        collectd.info("%s=%s" % (key, val))

    if module_config['metrics_key'] is None:
        raise ValueError("Missing required config setting: Metrics_Key")

    module_config['member_id'] = ("%s:%s" % (
        module_config['plugin_config']['Host'], module_config['plugin_config']['Port']))

    module_config['base_url'] = ("http://%s:%s/" %
                                 (module_config['plugin_config']['Host'], module_config['plugin_config']['Port']))

    if 'ssl_certificate' in module_config['ssl_keys'] and 'ssl_keyfile' in module_config['ssl_keys']:
        module_config['base_url'] = ('https' + module_config['base_url'][4:])

    if module_config['username'] is None and module_config['api_token'] is None:
        module_config['username'] = module_config['api_token'] = ''
    collectd.info("Using username '%s' and api_token '%s' " % (
        module_config['username'], module_config['api_token']))

    auth_handler = get_auth_handler(module_config)

    module_config['opener'] = urllib2.build_opener(auth_handler)

    collectd.debug("module_config: (%s)" % str(module_config))

    if testing:
        # for testing purposes
        return module_config

    if interval is not None:
        collectd.register_read(
            read_metrics,
            interval,
            data=module_config,
            name=module_config['member_id']
        )
    else:
        collectd.register_read(
            read_metrics,
            data=module_config,
            name=module_config['member_id']
        )


def str_to_bool(flag):
    '''
    Converts true/false to boolean
    '''
    flag = str(flag).strip().lower()
    if flag == 'true':
        return True
    elif flag != 'false':
        collectd.warning("WARNING: REQUIRES BOOLEAN. \
                RECEIVED %s. ASSUMING FALSE." % (str(flag)))

    return False


def prepare_plugin_instance(member_id, custom_dimensions, extra_dimensions=None):
    """
    Formats a dictionary of dimensions to a format that enables them to be
    specified as key, value pairs in plugin_instance to signalfx. E.g.
    dimensions = {'a': 'foo', 'b': 'bar'}
    _format_dimensions(dimensions)
    "[a=foo,b=bar]"
    Args:
    member_id: Unique id for each instance
    custom_dimensions (dict): Mapping of {dimension_name: value, ...} asked by user
    extra_dimensions (dict): Mapping of {dimension_name: value, ...} for jobs
    Returns:
    str: member_id[Comma-separated list of dimensions]
    """
    dim_pairs = []

    dim_pairs.extend("%s=%s" % (k, v) for k, v in custom_dimensions.iteritems())

    if extra_dimensions is not None:
        dim_pairs.extend("%s=%s" % (k, v) for k, v in extra_dimensions.iteritems())

    dim_str = ",".join(dim_pairs)

    if not dim_str:
        return "%s" % (member_id)

    return "%s[%s]" % (member_id, dim_str)


def prepare_and_dispatch_metric(module_config, name, value, _type, extra_dimensions=None):
    '''
    Prepares and dispatches a metric
    '''
    data_point = collectd.Values(plugin=PLUGIN_NAME)
    data_point.type_instance = name
    data_point.type = _type

    data_point.plugin_instance = prepare_plugin_instance(module_config['member_id'], module_config['custom_dimensions'],
                                                         extra_dimensions)

    data_point.values = [value]

    # With some versions of CollectD, a dummy metadata map must to be added
    # to each value for it to be correctly serialized to JSON by the
    # write_http plugin. See
    # https://github.com/collectd/collectd/issues/716
    data_point.meta = {'0': True}

    pprint_dict = {
        'plugin': data_point.plugin,
        'plugin_instance': data_point.plugin_instance,
        'type': data_point.type,
        'type_instance': data_point.type_instance,
        'values': data_point.values,
    }
    collectd.debug(pprint.pformat(pprint_dict))

    data_point.dispatch()


def read_and_post_job_metrics(module_config, url, job_name, last_timestamp):
    '''
    Reads json for a job and dispatches job related metrics
    '''
    job_url = url + 'job/' + job_name + '/'
    resp_obj = get_response(job_url, 'jenkins', module_config)
    extra_dimensions = {}
    extra_dimensions['Job'] = job_name
    if isinstance(resp_obj, dict) and resp_obj.get('builds', None) is not None:
        builds = resp_obj['builds']
        for i in xrange(len(builds)):
            build_url = job_url + str(builds[i]['number']) + '/'
            resp = get_response(build_url, 'jenkins', module_config)

            # Dispatch metrics only if build has completed
            if resp and not resp['building']:
                build_timestamp = resp['timestamp'] + resp['duration']

                # Dispatch metrics only if the timestamp is greater than that of
                # last metric sent else break as everything before it is already sent
                if build_timestamp > last_timestamp:
                    if module_config['jobs_last_timestamp'][job_name] < build_timestamp:
                        module_config['jobs_last_timestamp'][job_name] = build_timestamp

                    extra_dimensions['Result'] = resp['result']

                    for key in JOB_METRICS:
                        if key in resp:
                            prepare_and_dispatch_metric(
                                module_config,
                                JOB_METRICS[key].name,
                                resp[key],
                                JOB_METRICS[key].type,
                                extra_dimensions
                            )
                        else:
                            prepare_and_dispatch_metric(
                                module_config,
                                JOB_METRICS[key].name,
                                0,
                                JOB_METRICS[key].type,
                                extra_dimensions
                            )
                else:
                    break


def parse_and_post_metrics(module_config, resp):
    '''
    Read resposne and dispatch dropwizard metrics
    '''

    for key in NODE_METRICS:
        if key in resp:
            prepare_and_dispatch_metric(
                module_config,
                NODE_METRICS[key].name,
                resp[key]['value'],
                NODE_METRICS[key].type,
            )
        else:
            prepare_and_dispatch_metric(
                module_config,
                NODE_METRICS[key].name,
                0,
                NODE_METRICS[key].type,
            )

    # if the bool is true, then exclude metrics that are not required
    if module_config['enhanced_metrics']:
        for metric in resp:

            # metrics contains string and list as well which are not valid, hence skip
            if metric in module_config['exclude_optional_metrics'] or type(resp[metric]['value']) is str or \
                    type(resp[metric]['value']) is unicode or type(resp[metric]['value']) is list:
                continue

            prepare_and_dispatch_metric(
                module_config,
                metric,
                resp[metric]['value'],
                'gauge',
            )
    else:
        # include only the required metrics
        for metric in module_config['include_optional_metrics']:
            if metric in resp and not (type(resp[metric]['value']) is str or type(resp[metric]['value']) is unicode or
                                       type(resp[metric]['value']) is list):
                prepare_and_dispatch_metric(
                    module_config,
                    metric,
                    resp[metric]['value'],
                    'gauge',
                )


def parse_and_post_healthcheck(module_config, resp):
    '''
    Reads response and dispatches dropwizard healthcheck metrics
    '''

    for key in HEALTH_METRICS:
        if key in resp:
            prepare_and_dispatch_metric(
                module_config,
                HEALTH_METRICS[key].name,
                resp[key]['healthy'],
                HEALTH_METRICS[key].type,
            )
        else:
            prepare_and_dispatch_metric(
                module_config,
                HEALTH_METRICS[key].name,
                0,
                HEALTH_METRICS[key].type,
            )


def report_computer_status(module_config, computers_data):
    if computers_data and len(computers_data) > 1:
        for i in xrange(len(computers_data)):
            prepare_and_dispatch_metric(
                module_config,
                '%s-%s' % (COMPUTER_STATUS_METRICS['offline'].name, computers_data[i]['displayName']),
                not computers_data[i]['offline'],
                COMPUTER_STATUS_METRICS['offline'].type,
            )


def get_response(url, api_type, module_config):
    '''
    Prepare endpoint URL and get response
    '''

    extension = None
    resp_obj = None

    key = module_config['metrics_key']

    if api_type == 'jenkins':
        extension = 'api/json/'
    elif api_type == 'computer':
        extension = 'computer/api/json/'
    else:
        extension = 'metrics/%s/%s/' % (key, api_type)

    api_url = '%s%s' % (url, extension)
    collectd.debug('GET ' + api_url)

    if api_type == 'ping':
        resp_obj = ping_check(api_url, module_config['opener'], module_config['http_timeout'])
    else:
        resp_obj = _api_call(api_url, api_type, module_config['opener'], module_config['http_timeout'])

    if resp_obj is None:
        collectd.error('Unable to get data from %s for %s' % (api_url, api_type))

    return resp_obj


def read_metrics(module_config):
    '''
    Registered read call back function that collects
    metrics from all endpoints
    '''
    collectd.debug('Executing read_metrics callback')

    alive = get_response(module_config['base_url'], 'ping', module_config)

    if alive is not None:
        prepare_and_dispatch_metric(
            module_config,
            NODE_STATUS_METRICS['ping'].name,
            alive,
            NODE_STATUS_METRICS['ping'].type
        )

    if module_config['computer_metrics']:
        resp_obj = get_response(module_config['base_url'], 'computer', module_config)

        if resp_obj is not None:
            report_computer_status(module_config, resp_obj['computer'])

    resp_obj = get_response(module_config['base_url'], 'metrics', module_config)

    if resp_obj is not None:
        parse_and_post_metrics(module_config, resp_obj['gauges'])

    resp_obj = get_response(module_config['base_url'], 'healthcheck', module_config)

    if resp_obj is not None:
        parse_and_post_healthcheck(module_config, resp_obj)

    if module_config['job_metrics']:
        resp_obj = get_response(module_config['base_url'], 'jenkins', module_config)

        if resp_obj is not None:
            if "jobs" in resp_obj and resp_obj['jobs']:
                jobs_data = resp_obj['jobs']
                for job in jobs_data:
                    if job['name'] in module_config['jobs_last_timestamp']:
                        last_timestamp = module_config['jobs_last_timestamp'][job['name']]
                    else:
                        last_timestamp = int(time.time() * 1000) - (60 * 1000)
                        module_config['jobs_last_timestamp'][job['name']] = last_timestamp
                    read_and_post_job_metrics(module_config, module_config['base_url'], job['name'], last_timestamp)


def init():
    """
    The initialization callback is essentially a no-op for this plugin.
    """
    collectd.info("Initializing Jenkins plugin")


def shutdown():
    """
    The shutdown callback is essentially a no-op for this plugin.
    """
    collectd.info("Stopping jenkins plugin")


def setup_collectd():
    """
    Registers callback functions with collectd
    """
    collectd.register_init(init)
    collectd.register_config(read_config)
    collectd.register_shutdown(shutdown)


setup_collectd()
