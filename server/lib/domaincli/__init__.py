import cgi
import json
import logging
import os
import pymongo
import pymongo.objectid
import stripe
import urllib
import urllib2
import yaml

logger = logging.getLogger(__name__)

class Error(Exception):
    pass

class YourFault(Error):
    pass

class OurFault(Error):
    pass

class TheirFault(Error):
    pass

class WhoKnowsWhoseFault(Error):
    pass

class Config(object):
    @staticmethod
    def load():
        search = ['~/.domaincli-server', os.path.join(os.path.dirname(__file__), '../../conf.yaml')]
        for path in search:
            path = os.path.expanduser(path)
            if not os.path.exists(path):
                continue
            return yaml.load(open(path))
        raise OurFault('Could not find config file amongst search path of %r' % search)
config = Config.load()

class Translator(object):
    @classmethod
    def _get_answer(self, lookup, result):
        try:
            return lookup[result]
        except KeyError:
            raise OurFault("We can't for the life of us figure out what the upstream response '%s' means... but there's your answer" % result)

    @classmethod
    def check_availability(self, result):
        lookup = {'AVAILABLE' : True,
                  'UNAVAILABLE' : False,
                  'FAILURE' : None }
        return self._get_answer(lookup, result)

    @classmethod
    def register_domain(self, result):
        lookup = {'SUCCESS' : True,
                  'FAILURE' : False }
        return self._get_answer(lookup, result)
    
    @classmethod
    def set_nameservers(self, result):
        lookup = {'SUCCESS' : True,
                  'FAILURE' : False }
        return self._get_answer(lookup, result)

class DomainCLI(object):
    API_URL = 'https://testapi.internet.bs/'

    def __init__(self, api_key=None, password=None):
        self.api_key = api_key or config['api_key']
        if not self.api_key:
            raise WhoKnowsWhoseFault('No api_key provided')
        self.password = password or config['password']
        if not self.password:
            raise WhoKnowsWhoseFault('No password provided')
        self.db = pymongo.Connection().domaincli

    # Stolen from Stripe
    def _encodeInner(self, d):
        """
        We want post vars of form:
        {'foo': 'bar', 'nested': {'a': 'b', 'c': 'd'}}
        to become:
        foo=bar&nested[a]=b&nested[c]=d
        """
        stk = []
        for key, value in d.items():
            if isinstance(value, dict):
                n = {}
                for k, v in value.items():
                    n["%s[%s]" % (key, k)] = v
                    stk.extend(self._encodeInner(n))
            else:
                stk.append((key, value))
        return stk

    # Stolen from Stripe
    def _encode(self, d):
        """
        Internal: encode a string for url representation
        """
        return urllib.urlencode(self._encodeInner(d))


    def _call(self, path, **params):
        uri = self.API_URL + path
        params['apikey'] = self.api_key
        params['password'] = self.password
        params['ResponseFormat'] = 'json'
        post_body = self._encode(params)
        print '----> Calling %s with args %r' % (path, params)
        try:
            c = urllib2.urlopen(uri, post_body)
        except urllib2.URLError, e:
            # TODO: catch these before the client gets them
            raise WhoKnowsWhoseFault("Unexpected error: %s" % (e, ))
        resp_str = c.read()
        print "----> Full result: %r" % resp_str
        resp = json.loads(resp_str)
        return resp

    def rpc_check_availability(self, params):
        domain = params['domain']
        result = self._call('Domain/Check', domain=domain)
        availability = Translator.check_availability(result['status'])
        if availability is not None:
            return {
                'object' : 'result',
                'available' : availability
                }
        else:
            return {
                'object' : 'error',
                'message' : result['message']
                }

    def rpc_register_domain(self, params):
        tlds = ['com', 'info', 'net', 'org', 'us']
        domain = params['domain']
        if not any(domain.endswith('.' + tld) for tld in tlds):
            raise YourFault('Sorry, we currently only support the following TLDs: %s' % ', '.join(tlds))
        user = self.get_user(params)
        # TODO: a bit more defensive here
        # card = user['card_hashes'][-1]
        # sclient = Stripe(config['stripe_api_key'])
        # sclient.execute(amount=1200, currency='usd', card=card['id'])
        years = '%dy' % int(params['years'])
        # TODO: not everything supports private whois:
        # 'privateWhois' : 'FULL'
        # but it's on by default now, so there's that
        params = { 'domain' : domain,
                   'period' : years }
        contacts = ['Registrant', 'Admin', 'Technical', 'Billing']
        fields = { 'FirstName' : 'Domain',
                   'LastName' : 'Registrant',
                   'Email' : '%s@domaincli.com' % domain,
                   'PhoneNumber' : '+1.7104192312',
                   'Street' : '701 Webster St',
                   'City' : 'Palo Alto',
                   'CountryCode' : 'US',
                   'PostalCode' : '94301' }
        for contact in contacts:
            for field, value in fields.iteritems():
                params['%s_%s' % (contact, field)] = value
        result = self._call('Domain/Create', **params)
        try:
            success = Translator.register_domain(result['status'])
        except KeyError:
            success = Translator.register_domain(result['product'][0]['status'])
        if success:
            assert(result['currency'] == 'USD')
            assert(result['product'][0]['domain'] == domain)
            domains = user.setdefault('domains', [])
            domains.append(domain)
            self.db.users.update({'_id' : user['_id']}, {'$set' : {'domains' : domains }})
            return {
                'object' : 'result',
                'success' : True,
                'message' : 'Congatulations!  You now own %s.' % (domain, )
                }
        else:
            return {
                'object' : 'result',
                'success' : False,
                'message' : result['message']
                }

    def rpc_set_nameservers(self, params):
        user = self.get_user(params)
        nameservers = params['nameservers']
        domain = params['domain']
        if domain not in user['domains']:
            return {
                'object' : 'error',
                'message' : "Sorry, you don't appear to own that domain.  Feel free to contact us at gdb@gregbrockman.com if we're mistaken."
                }
        # TODO: set up deletion
        errors = []
        successes = []
        for ns in nameservers.split(','):
            ns = ns.strip()
            res = self._call('Domain/DnsRecord/Add', type='NS', value=ns, fullrecordname=domain)
            if Translator.set_nameservers(res['status']):
                successes.append(ns)
            else:
                errors.append(res['message'])
        if errors:
            msg = ' '.join(errors)
            if successes:
                msg += " But that's ok, because I successfully set the following DNS servers: %s" % ', '.join(successes)
            return {
                'object' : 'error',
                'message' : msg
                }
        else:
            return {
                'object' : 'result',
                'message' : 'Set nameservers to %s' % nameservers
                }

    def rpc_domaincli_create_account(self, params):
        objid = self.db.users.insert({})
        return {
            'object' : 'result',
            'success' : True,
            'id' : str(objid)
            }

    def rpc_domaincli_get_card(self, params):
        user = self.get_user(params)
        try:
            hash = user['card_hashes']
        except KeyError:
            return {
                'object' : 'result',
                'success' : False
                }
        else:
            return hash[-1]

    def rpc_domaincli_add_card(self, params):
        # TODO: do more once retrieve_card is a thing
        user = self.get_user(params)
        card_token = params['card_token']
        card_hashes = user.setdefault('card_hashes', [])
        card_hashes.append({
                'object' : 'card',
                'id' : card_token
                })
        self.db.users.update({'_id' : user['_id']}, {'$set' : {'card_hashes' : card_hashes }})
        return {
            'object' : 'result'
            }

    def private_price_list(self, params):
        result = self._call('Account/PriceList/Get')
        print repr(result)
        return result

    def get_user(self, params):
        try:
            user_id = params['user_id']
        except KeyError:
            raise YourFault('Missing user_id.  Seems like a bug in the client library?')
        try:
            return self.db.users.find({'_id' : pymongo.objectid.ObjectId(user_id)})[0]
        except IndexError:
            raise YourFault('Invalid user_id.  Check your config file (~/.domaincli by default)')
