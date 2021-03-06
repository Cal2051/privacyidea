# -*- coding: utf-8 -*-
#
#  2018-01-21 Cornelius Kölbel <cornelius.koelbel@netknights.it>
#             Add tokenkind
#  2016-02-22 Cornelius Kölbel <cornelius@privacyidea.org>
#             Add the RADIUS identifier, which points to the system wide list
#             of RADIUS servers.
#  2015-10-09 Cornelius Kölbel <cornelius@privacyidea.org>
#             Add the RADIUS-System-Config, so that not each
#             RADIUS-token needs his own secret. -> change the
#             secret globally
#  2015-01-29 Adapt for migration to flask
#             Cornelius Kölbel <cornelius@privacyidea.org>
#
#  May 08, 2014 Cornelius Kölbel
#  License:  AGPLv3
#  contact:  http://www.privacyidea.org
#
#  Copyright (C) 2010 - 2014 LSE Leading Security Experts GmbH
#  License:  LSE
#  contact:  http://www.linotp.org
#            http://www.lsexperts.de
#            linotp@lsexperts.de
#
# This code is free software; you can redistribute it and/or
# modify it under the terms of the GNU AFFERO GENERAL PUBLIC LICENSE
# License as published by the Free Software Foundation; either
# version 3 of the License, or any later version.
#
# This code is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU AFFERO GENERAL PUBLIC LICENSE for more details.
#
# You should have received a copy of the GNU Affero General Public
# License along with this program.  If not, see <http://www.gnu.org/licenses/>.
#
__doc__ = """This module defines the RadiusTokenClass. The RADIUS token
forwards the authentication request to another RADIUS server.

The code is tested in tests/test_lib_tokens_radius
"""

import logging

import traceback
import binascii
from privacyidea.lib.utils import is_true, to_bytes, hexlify_and_unicode, to_unicode
from privacyidea.lib.tokenclass import TokenClass, TOKENKIND
from privacyidea.api.lib.utils import getParam, ParameterError
from privacyidea.lib.log import log_with
from privacyidea.lib.config import get_from_config
from privacyidea.lib.decorators import check_token_locked
from privacyidea.lib.policydecorators import challenge_response_allowed
from privacyidea.lib.radiusserver import get_radius
from privacyidea.models import Challenge
from privacyidea.lib.challenge import get_challenges

import pyrad.packet
from pyrad.client import Client
from pyrad.dictionary import Dictionary
from privacyidea.lib import _

optional = True
required = False

log = logging.getLogger(__name__)


###############################################
class RadiusTokenClass(TokenClass):

    def __init__(self, db_token):
        TokenClass.__init__(self, db_token)
        self.set_type(u"radius")
        self.mode = ['authenticate', 'challenge']

    @staticmethod
    def get_class_type():
        return "radius"

    @staticmethod
    def get_class_prefix():
        return "PIRA"

    @staticmethod
    @log_with(log)
    def get_class_info(key=None, ret='all'):
        """
        returns a subtree of the token definition

        :param key: subsection identifier
        :type key: string
        :param ret: default return value, if nothing is found
        :type ret: user defined
        :return: subsection if key exists or user defined
        :rtype: dict or string
        """
        res = {'type': 'radius',
               'title': 'RADIUS Token',
               'description': _('RADIUS: Forward authentication request to a '
                                'RADIUS server.'),
               'user': ['enroll'],
               # This tokentype is enrollable in the UI for...
               'ui_enroll': ["admin", "user"],
               'policy': {},
               }

        if key:
            ret = res.get(key, {})
        else:
            if ret == 'all':
                ret = res

        return ret

    @log_with(log)
    def update(self, param):
        # New value
        radius_identifier = getParam(param, "radius.identifier")
        self.add_tokeninfo("radius.identifier", radius_identifier)

        # old values
        if not radius_identifier:
            radiusServer = getParam(param, "radius.server", optional=required)
            self.add_tokeninfo("radius.server", radiusServer)
            radius_secret = getParam(param, "radius.secret", optional=required)
            self.token.set_otpkey(hexlify_and_unicode(radius_secret))
            system_settings = getParam(param, "radius.system_settings",
                                       default=False)
            self.add_tokeninfo("radius.system_settings", system_settings)

            if not (radiusServer or radius_secret) and not system_settings:
                raise ParameterError("Missing parameter: radius.identifier", id=905)

        # if another OTP length would be specified in /admin/init this would
        # be overwritten by the parent class, which is ok.
        self.set_otplen(6)
        self.update(self, param)
        val = getParam(param, "radius.local_checkpin", optional) or 0
        self.add_tokeninfo("radius.local_checkpin", val)

        val = getParam(param, "radius.user", required)
        self.add_tokeninfo("radius.user", val)
        self.add_tokeninfo("tokenkind", TOKENKIND.VIRTUAL)

    @log_with(log)
    def is_challenge_request(self, passw, user=None, options=None):
        """
        This method checks, if this is a request, that triggers a challenge.
        It depends on the way, the pin is checked - either locally or remote

        :param passw: password, which might be pin or pin+otp
        :type passw: string
        :param user: The user from the authentication request
        :type user: User object
        :param options: dictionary of additional request parameters
        :type options: dict

        :return: true or false
        """
        options = options or {}
        otp_counter = -1
        otpval = passw
        state = options.get('radius_state')

        # should we check the pin locally?
        if self.check_pin_local:
            (_res, pin, otpval) = self.split_pin_pass(passw, user,
                                                      options=options)
            if not self.check_pin(self, pin):
                return False

        # challenge is still valid
        result = options.get('radius_result')
        if result is None:
            res = self.check_radius(passw, options=options, radius_state=state)
        else:
            res = result

        return res == 1

    @log_with(log)
    def create_challenge(self, transactionid=None, options=None):
        """
        create a challenge, which is submitted to the user

        :param transactionid: the id of this challenge
        :param options: the request context parameters / data
        :return: tuple of (bool, message and data)
                 bool, if submit was successful
                 message is submitted to the user
                 data is preserved in the challenge
                 attributes - additional attributes, which are displayed in the
                    output
        """
        options = options or {}
        message = options.get('radius_message') or "Enter your RADIUS tokencode:"
        state = options.get('radius_state')
        attributes = {'state': transactionid}
        validity = int(get_from_config('DefaultChallengeValidityTime', 120))

        db_challenge = Challenge(self.token.serial,
                                 transaction_id=transactionid,
                                 data=state,
                                 challenge=message,
                                 validitytime=validity)
        db_challenge.save()
        self.challenge_janitor()
        return True, message, db_challenge.transaction_id, attributes

    @log_with(log)
    def is_challenge_response(self, passw, user=None, options=None):
        """
        This method checks, if this is a request, that is the response to
        a previously sent challenge.

        Since this is RADIUS, we need to additionally check to see if the
        current request is going to spawn another challenge

        :param passw: password, which might be pin or pin+otp
        :type passw: string
        :param user: the requesting user
        :type user: User object
        :param options: dictionary of additional request parameters
        :type options: dict
        :return: true or false
        :rtype: bool
        """
        options = options or {}
        otpval = passw
        challenge_response = False

        # clear the radius_result since this is the first function called in the chain
        # this value will be utilized to ensure we do not check_radius more than once in the loop
        options.update({'radius_result': None})

        # fetch the transaction_id
        transaction_id = options.get('transaction_id')
        if transaction_id is None:
            transaction_id = options.get('state')

        if transaction_id:
            # get the challenges for this transaction ID
            challengeobject_list = get_challenges(serial=self.token.serial,
                                                  transaction_id=transaction_id)

            for challengeobject in challengeobject_list:
                if challengeobject.is_valid():
                    # challenge is still valid, pull the state and message and other data
                    state = challengeobject.data

                    # we need to check against radius before continuing
                    # this causes a bit of an issue since we will know
                    # if the challenge is successful before we try
                    res = self.check_radius(otpval, options=options, radius_state=state)
                    if res != 1:  # we are not getting challenged again so we can stop the chain
                        challenge_response = True
                    else:
                        challengeobject.delete()
                        self.challenge_janitor()

        return challenge_response

    @log_with(log)
    @check_token_locked
    def check_challenge_response(self, user=None, passw=None, options=None):
        """
        This method verifies if there is a matching question for the given
        passw and also verifies if the answer is correct.

        It then returns the the otp_counter = 1

        :param user: the requesting user
        :type user: User object
        :param passw: the password - in fact it is the answer to the question
        :type passw: string
        :param options: additional arguments from the request, which could
                        be token specific. Usually "transaction_id"
        :type options: dict
        :return: return otp_counter. If -1, challenge does not match
        :rtype: int
        """
        options = options or {}
        otp_counter = -1

        # fetch the transaction_id
        transaction_id = options.get('transaction_id')
        if transaction_id is None:
            transaction_id = options.get('state')

        # get the challenges for this transaction ID
        if transaction_id is not None:
            challengeobject_list = get_challenges(serial=self.token.serial,
                                                  transaction_id=transaction_id)

            for challengeobject in challengeobject_list:
                if challengeobject.is_valid():
                    state = challengeobject.data

                    # challenge is still valid
                    otp_counter = self.check_radius(passw, options=options, radius_state=state)
                    if otp_counter == 0:
                        # We found the matching challenge, so lets return the
                        #  successful result and delete the challenge object.
                        challengeobject.delete()
                        break
                    else:
                        otp_counter = -1
                        # increase the received_count
                        challengeobject.set_otp_status()

        self.challenge_janitor()
        return otp_counter

    @property
    def check_pin_local(self):
        """
        lookup if pin should be checked locally or on radius host

        :return: bool
        """
        local_check = is_true(self.get_tokeninfo("radius.local_checkpin"))
        log.debug("local checking pin? {0!r}".format(local_check))

        return local_check

    @log_with(log)
    def split_pin_pass(self, passw, user=None, options=None):
        """
        Split the PIN and the OTP value.
        Only if it is locally checked and not remotely.
        """
        res = 0
        pin = ""
        otpval = passw
        if self.check_pin_local:
            (res, pin, otpval) = self.split_pin_pass(self, passw)

        return res, pin, otpval

    @log_with(log)
    @check_token_locked
    def authenticate(self, passw, user=None, options=None):
        """
        do the authentication on base of password / otp and user and
        options, the request parameters.

        Here we contact the other privacyIDEA server to validate the OtpVal.

        :param passw: the password / otp
        :param user: the requesting user
        :param options: the additional request parameters

        :return: tuple of (success, otp_count - 0 or -1, reply)

        """
        options = options or {}
        res = False
        otp_counter = -1
        reply = None
        otpval = passw

        # should we check the pin localy?
        if self.check_pin_local:
            (_res, pin, otpval) = self.split_pin_pass(passw, user,
                                                      options=options)

            if not self.check_pin(self, pin):
                return False, otp_counter, {'message': "Wrong PIN"}

        # attempt to retreive saved state/result
        state = options.get('radius_state')
        result = options.get('radius_result')
        if result is None:
            otp_counter = self.check_radius(otpval, options=options, radius_state=state)
        else:
            otp_counter = result

        if otp_counter == 0:
            res = True
            reply = {'message': 'matching 1 tokens',
                     'serial': self.get_serial(),
                     'type': self.get_tokentype()}
        elif otp_counter == 1:  # challenge required but not handled (error state)
            reply = {'message': 'unexpected challenge required'}
        else:
            reply = {'message': 'remote side denied access'}

        return res, otp_counter, reply

    @log_with(log)
    @check_token_locked
    def check_otp(self, otpval, counter=None, window=None, options=None):
        res = self.check_radius(otpval, options=options)
        return res != -1

    @log_with(log)
    @check_token_locked
    def check_radius(self, otpval, options=None, radius_state=None):
        """
        run the RADIUS request against the RADIUS server

        :param otpval: the OTP value
        :param options: additional token specific options
        :type options: dict
        :return: counter of the matching OTP value.
        :rtype: success/failure/challenge (0,-1,1)
        """
        result = -1
        radius_message = None
        options = options or {}

        # check to see if a previous check was already a success or failure
        authstate = options.get('radius_state')
        if authstate == '<SUCCESS>':
            options.update({'radius_state': None})
            options.update({'radius_message': None})
            return 0
        elif authstate == '<REJECTED>':
            options.update({'radius_state': None})
            options.update({'radius_message': None})
            return -1

        radius_dictionary = None
        radius_identifier = self.get_tokeninfo("radius.identifier")
        radius_user = self.get_tokeninfo("radius.user")
        system_radius_settings = self.get_tokeninfo("radius.system_settings")
        if radius_identifier:
            # New configuration
            radius_server_object = get_radius(radius_identifier)
            radius_server = radius_server_object.config.server
            radius_port = radius_server_object.config.port
            radius_server = u"{0!s}:{1!s}".format(radius_server, radius_port)
            radius_secret = radius_server_object.get_secret()
            radius_dictionary = radius_server_object.config.dictionary

        elif system_radius_settings:
            # system configuration
            radius_server = get_from_config("radius.server")
            radius_secret = get_from_config("radius.secret")
        else:
            # individual token settings
            radius_server = self.get_tokeninfo("radius.server")
            # Read the secret
            secret = self.token.get_otpkey()
            radius_secret = binascii.unhexlify(secret.getKey())

        # here we also need to check for radius.user
        log.debug(u"checking OTP len:{0!s} on radius server: "
                  u"{1!s}, user: {2!r}".format(len(otpval), radius_server,
                                               radius_user))

        try:
            # pyrad does not allow to set timeout and retries.
            # it defaults to retries=3, timeout=5

            # TODO: At the moment we support only one radius server.
            # No round robin.
            server = radius_server.split(':')
            r_server = server[0]
            r_authport = 1812
            if len(server) >= 2:
                r_authport = int(server[1])
            nas_identifier = get_from_config("radius.nas_identifier",
                                             "privacyIDEA")
            if not radius_dictionary:
                radius_dictionary = get_from_config("radius.dictfile",
                                                    "/etc/privacyidea/dictionary")
            log.debug(u"NAS Identifier: %r, "
                      u"Dictionary: %r" % (nas_identifier, radius_dictionary))
            log.debug(u"constructing client object "
                      u"with server: %r, port: %r, secret: %r" %
                      (r_server, r_authport, to_unicode(radius_secret)))

            srv = Client(server=r_server,
                         authport=r_authport,
                         secret=to_bytes(radius_secret),
                         dict=Dictionary(radius_dictionary))

            req = srv.CreateAuthPacket(code=pyrad.packet.AccessRequest,
                                       User_Name=radius_user.encode('utf-8'),
                                       NAS_Identifier=nas_identifier.encode('ascii'))

            req["User-Password"] = req.PwCrypt(otpval)
            if "transactionid" in options:
                req["State"] = str(options.get("transactionid"))

            if radius_state:
                req["State"] = str(radius_state)
                log.info("Sending saved challenge to radius server: {0} ".format(radius_state))

            response = srv.SendPacket(req)
            # handle the RADIUS challenge
            if response.code == pyrad.packet.AccessChallenge:
                opt = {}
                for attr in response.keys():
                    opt[attr] = response[attr]
                res = False
                log.info("Radiusserver challenge returned %s '%s' " % (opt["State"], opt["Reply-Message"]))
                # now we map this to a privacyidea challenge
                if "State" in opt:
                    radius_state = opt["State"][0]
                if "Reply-Message" in opt:
                    radius_message = opt["Reply-Message"][0]

                result = 1
            elif response.code == pyrad.packet.AccessAccept:
                radius_state = '<SUCCESS>'
                radius_message = 'RADIUS authentication succeeded'
                log.info("Radiusserver %s granted "
                         "access to user %s." % (r_server, radius_user))
                result = 0
            else:
                radius_state = '<REJECTED>'
                radius_message = 'RADIUS authentication failed'
                log.debug('radius response code %d' % response.code)
                log.info("Radiusserver %s "
                            "rejected access to user %s." %
                            (r_server, radius_user))

        except Exception as ex:  # pragma: no cover
            log.error("Error contacting radius Server: {0!r}".format((ex)))
            log.info("{0!s}".format(traceback.format_exc()))

        options.update({'radius_result': result})
        options.update({'radius_state': radius_state})
        options.update({'radius_message': radius_message})
        return result
