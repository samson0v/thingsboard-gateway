#     Copyright 2025. ThingsBoard
#
#     Licensed under the Apache License, Version 2.0 (the "License");
#     you may not use this file except in compliance with the License.
#     You may obtain a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#     Unless required by applicable law or agreed to in writing, software
#     distributed under the License is distributed on an "AS IS" BASIS,
#     WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#     See the License for the specific language governing permissions and
#     limitations under the License.

import random
import socket
import ssl
import string
import threading
import inspect
from logging import getLogger
from os import environ
from os.path import exists, join, abspath, dirname
from sys import path
from ssl import CERT_REQUIRED
from copy import deepcopy
from time import sleep, time
from typing import Union

from simplejson import dumps, load

from thingsboard_gateway.gateway.constants import DEV_MODE_PARAMETER_NAME
from thingsboard_gateway.tb_utility.tb_utility import TBUtility

try:
    if environ.get(DEV_MODE_PARAMETER_NAME) is not None and environ.get(DEV_MODE_PARAMETER_NAME).lower() == 'true':
        raise ImportError
    from tb_gateway_mqtt import TBGatewayMqttClient, TBDeviceMqttClient, \
        GATEWAY_ATTRIBUTES_RESPONSE_TOPIC, GATEWAY_ATTRIBUTES_TOPIC
    import tb_device_mqtt
except ImportError:
    mqtt_client_path = abspath(join(dirname(__file__), '..', '..', 'tb_mqtt_client'))
    from os import environ
    if exists(mqtt_client_path) and TBUtility.str_to_bool(environ.get(DEV_MODE_PARAMETER_NAME, 'false')):
        path.insert(0, mqtt_client_path)
        from tb_gateway_mqtt import TBGatewayMqttClient, TBDeviceMqttClient, \
            GATEWAY_ATTRIBUTES_RESPONSE_TOPIC, GATEWAY_ATTRIBUTES_TOPIC
        import tb_device_mqtt
    else:
        print("tb-mqtt-client library not found - installing...")
        TBUtility.install_package('tb-mqtt-client')
        from tb_gateway_mqtt import TBGatewayMqttClient, TBDeviceMqttClient, \
            GATEWAY_ATTRIBUTES_RESPONSE_TOPIC, GATEWAY_ATTRIBUTES_TOPIC
        import tb_device_mqtt

tb_device_mqtt.DEFAULT_TIMEOUT = 3


class TBClient(threading.Thread):
    def __init__(self, config, config_folder_path, logger):
        self.__logger = logger
        super().__init__()
        self.name = 'Connection thread.'
        self.daemon = True
        self.__config_folder_path = config_folder_path
        self.__config = config
        self.__host = config["host"]
        self.__port = config.get("port", 1883)
        self.__default_quality_of_service = config.get("qos", 1)
        self.__min_reconnect_delay = 1
        self.client: Union[TBGatewayMqttClient, None] = None
        self.__ca_cert = None
        self.__private_key = None
        self.__cert = None
        self.__client_id = ""
        self.__username = None
        self.__password = None
        self.__is_connected = False
        self.__stopped = False
        self.__stop_event = threading.Event()
        self.__paused = False
        # TODO: remove this flag
        self.__initial_connection_done = False
        self._last_cert_check_time = 0
        self.__service_subscription_callbacks = []

        # check if provided creds or provisioning strategy
        if config.get('security'):
            self._create_mqtt_client(config['security'])
        elif config.get('provisioning'):
            if exists(self.__config_folder_path + 'credentials.json'):
                with open(self.__config_folder_path + 'credentials.json', 'r') as file:
                    credentials = load(file)
                creds = self._get_provisioned_creds(credentials)
            else:
                credentials = config['provisioning']
                logger.info('Starting provisioning gateway...')

                credentials_type = credentials.pop('type', 'ACCESS_TOKEN')
                if credentials_type.upper() == 'ACCESS_TOKEN':
                    credentials['access_token'] = ''.join(random.choice(string.ascii_lowercase) for _ in range(15))
                elif credentials_type.upper() == 'MQTT_BASIC':
                    credentials['client_id'] = ''.join(random.choice(string.ascii_lowercase) for _ in range(15))
                    credentials['username'] = ''.join(random.choice(string.ascii_lowercase) for _ in range(15))
                    credentials['password'] = ''.join(random.choice(string.ascii_lowercase) for _ in range(15))
                elif credentials_type.upper() == 'X509_CERTIFICATE':
                    self._ca_cert_name = credentials.pop('caCert')
                    new_cert_path = self.__config_folder_path + 'cert.pem'
                    new_private_key_path = self.__config_folder_path + 'key.pem'
                    gen_hash = TBUtility.generate_certificate(new_cert_path, new_private_key_path).decode('utf-8')
                    credentials['hash'] = gen_hash
                else:
                    raise RuntimeError('Unknown provisioning type '
                                       '(Available options: AUTO, ACCESS_TOKEN, MQTT_BASIC, X509_CERTIFICATE)')

                gateway_name = 'Gateway ' + ''.join(random.choice(string.ascii_lowercase) for _ in range(5))
                prov_gateway_key = credentials.pop('provisionDeviceKey')
                prov_gateway_secret = credentials.pop('provisionDeviceSecret')
                creds = TBDeviceMqttClient.provision(host=self.__host,
                                                     port=1883,
                                                     device_name=gateway_name,
                                                     provision_device_key=prov_gateway_key,
                                                     provision_device_secret=prov_gateway_secret,
                                                     **credentials)

                with open(self.__config_folder_path + 'credentials.json', 'w') as file:
                    creds['caCert'] = self._ca_cert_name
                    file.writelines(dumps(creds))
                logger.info('Gateway provisioned')

                creds = self._get_provisioned_creds(creds)

            self._create_mqtt_client(creds)
        else:
            raise RuntimeError('Security section not provided')

        # pylint: disable=protected-access
        # Adding callbacks
        self.client._client._on_connect = self._on_connect  # noqa pylint: disable=protected-access
        self.client._client._on_disconnect = self._on_disconnect  # noqa pylint: disable=protected-access
        # self.client._client._on_log = self._on_log  # noqa pylint: disable=protected-access
        self.start()

    # def _on_log(self, *args):
    #     if "exception" in args[-1]:
    #         self.__logger.exception(args)
    #     else:
    #         self.__logger.debug(args)

    def _create_mqtt_client(self, credentials):
        env_vars = TBUtility.get_service_environmental_variables()
        self.__tls = bool((credentials.get('tls', False) or
                          credentials.get('caCert', False)))
        if env_vars.get('caCert') is not None:
            self.__tls = True
        if env_vars.get('type') is not None:
            credentials_type = env_vars.get('type')
        else:
            credentials_type = credentials.get('type')
        if credentials_type is None and credentials.get('accessToken') is not None:
            credentials_type = 'accessToken'
        if credentials_type == 'accessToken':
            if env_vars.get('accessToken') is not None:
                self.__username = str(env_vars["accessToken"])
            else:
                self.__username = str(credentials["accessToken"])
        if credentials_type == 'usernamePassword':
            if credentials.get("username") is not None:
                if env_vars.get('username') is not None:
                    self.__username = str(env_vars["username"])
                else:
                    self.__username = str(credentials["username"])
            if credentials.get("password") is not None:
                if env_vars.get('password') is not None:
                    self.__password = str(env_vars["password"])
                else:
                    self.__password = str(credentials["password"])
            if credentials.get("clientId") is not None:
                if env_vars.get('clientId') is not None:
                    self.__client_id = str(env_vars["clientId"])
                else:
                    self.__client_id = str(credentials["clientId"])

        rate_limits_config = self.__get_rate_limit_config()

        if rate_limits_config:
            self.client = TBGatewayMqttClient(self.__host, self.__port, self.__username, self.__password, self,
                                              quality_of_service=self.__default_quality_of_service,
                                              client_id=self.__client_id, **rate_limits_config)
        else:
            self.client = TBGatewayMqttClient(self.__host, self.__port, self.__username, self.__password, self,
                                              quality_of_service=self.__default_quality_of_service,
                                              client_id=self.__client_id)

        self.__ca_cert = self.__get_path_to_cert(credentials.get("caCert")) \
            if credentials.get("caCert") is not None else None
        if env_vars.get('caCert') is not None:
            self.__ca_cert = self.__get_path_to_cert(env_vars.get("caCert"))
        self.__private_key = self.__get_path_to_cert(credentials.get("privateKey")) \
            if credentials.get("privateKey") is not None else None
        if env_vars.get('privateKey') is not None:
            self.__private_key = self.__get_path_to_cert(env_vars.get("privateKey"))
        self.__cert = self.__get_path_to_cert(credentials.get("cert")) \
            if credentials.get("cert") is not None else None
        if env_vars.get('cert') is not None:
            self.__cert = self.__get_path_to_cert(env_vars.get("cert"))
        self.__check_cert_period = credentials.get('checkCertPeriod', 86400)
        self.__certificate_days_left = credentials.get('certificateDaysLeft', 3)

        if self.__tls:
            # check certificates for end date
            self._check_cert_thread = threading.Thread(name='Check Certificates Thread',
                                                       target=self._check_certificates,
                                                       daemon=True)
            self._check_cert_thread.start()

        cert_required = CERT_REQUIRED if (self.__ca_cert and
                                          self.__cert) else ssl.CERT_OPTIONAL if self.__cert else ssl.CERT_NONE

        self.client._client.tls_set(ca_certs=self.__ca_cert,
                                    certfile=self.__cert,
                                    keyfile=self.__private_key,
                                    tls_version=ssl.PROTOCOL_TLSv1_2,
                                    cert_reqs=cert_required,
                                    ciphers=None)  # noqa pylint: disable=protected-access
        if credentials.get("insecure", False):
            self.client._client.tls_insecure_set(True)  # noqa pylint: disable=protected-access
        if self.__logger.isEnabledFor(10):
            self.client._client.enable_logger(self.__logger)  # noqa pylint: disable=protected-access

    def __get_rate_limit_config(self):
        rate_limits_config = {}
        if self.__config.get('messagesRateLimits'):
            rate_limits_config['messages_rate_limit'] = self.__config['messagesRateLimits']
        if self.__config.get('telemetryRateLimits'):
            rate_limits_config['telemetry_rate_limit'] = self.__config['rateLimits']
        if self.__config.get('telemetryDpRateLimits'):
            rate_limits_config['telemetry_dp_rate_limit'] = self.__config['dpRateLimits']

        if self.__config.get('deviceMessagesRateLimits'):
            rate_limits_config['device_messages_rate_limit'] = self.__config['deviceMessagesRateLimits']
        if self.__config.get('deviceTelemetryRateLimits'):
            rate_limits_config['device_telemetry_rate_limit'] = self.__config['deviceRateLimits']
        if self.__config.get('deviceTelemetryDpRateLimits'):
            rate_limits_config['device_telemetry_dp_rate_limit'] = self.__config['deviceDpRateLimits']

        if 'rate_limit' in inspect.signature(TBGatewayMqttClient.__init__).parameters:
            rate_limits_config = {}
            if self.__config.get('rateLimits'):
                rate_limits_config['rate_limit'] = 'DEFAULT_RATE_LIMIT' if self.__config.get(
                    'rateLimits') == 'DEFAULT_TELEMETRY_RATE_LIMIT' else self.__config['rateLimits']
            if ('dp_rate_limit' in inspect.signature(TBGatewayMqttClient.__init__).parameters and
                    self.__config.get('dpRateLimits')):
                rate_limits_config['dp_rate_limit'] = 'DEFAULT_RATE_LIMIT' if self.__config[
                    'dpRateLimits'] == 'DEFAULT_TELEMETRY_DP_RATE_LIMIT' else self.__config['dpRateLimits']

        return rate_limits_config

    def __get_path_to_cert(self, filename):
        if exists(self.__config_folder_path + filename):
            return self.__config_folder_path + filename
        else:
            return filename

    @staticmethod
    def _get_provisioned_creds(credentials):
        creds = {}
        if credentials.get('credentialsType') == 'ACCESS_TOKEN':
            creds['accessToken'] = credentials['credentialsValue']
        elif credentials.get('credentialsType') == 'MQTT_BASIC':
            creds['clientId'] = credentials['credentialsValue']['clientId']
            creds['username'] = credentials['credentialsValue']['userName']
            creds['password'] = credentials['credentialsValue']['password']
        elif credentials.get('credentialsType') == 'X509_CERTIFICATE':
            creds['tls'] = True
            creds['caCert'] = credentials['caCert']
            creds['privateKey'] = 'key.pem'
            creds['cert'] = 'cert.pem'

        return creds

    def _check_certificates(self):
        while not self.__stopped and not self.__paused:
            if time() - self._last_cert_check_time >= self.__check_cert_period:
                if self.__cert:
                    self.__logger.info('Will generate new certificate')
                    new_cert = TBUtility.check_certificate(self.__cert, key=self.__private_key,
                                                           days_left=self.__certificate_days_left)

                    if new_cert:
                        self.client.send_attributes({'newCertificate': new_cert})

                if self.__ca_cert:
                    is_outdated = TBUtility.check_certificate(self.__ca_cert, generate_new=False,
                                                              days_left=self.__certificate_days_left)

                    if is_outdated:
                        self.client.send_attributes({'CACertificate': 'CA certificate will outdated soon'})

                self._last_cert_check_time = time()

            sleep(10)

    def pause(self):
        self.__paused = True

    def unpause(self):
        self.__paused = False

    def is_connected(self):
        return self.__is_connected and self.client.rate_limits_received

    def _on_connect(self, client, userdata, flags, result_code, parameters, *extra_params):
        self.__logger.info('MQTT client connected to platform %s: %s', self.__host, self.__port)
        self.__logger.debug('MQTT client %r connected to platform', str(client))
        if ((isinstance(result_code, int) and result_code == 0) or
                (hasattr(result_code, 'value') and result_code.value == 0)):
            self.__is_connected = True
            if self.__initial_connection_done:
                self.client.rate_limits_received = True  # Added to avoid stuck on reconnect, if rate limits reached,
                # TODO: move to high priority.
            else:
                self.__initial_connection_done = True
        # pylint: disable=protected-access
        self.client._on_connect(client, userdata, flags, result_code, parameters, *extra_params) # noqa pylint: disable=protected-access
        try:
            if isinstance(result_code, int) and result_code != 0:
                if result_code in (159, 151):
                    self.__logger.warning("Connection rate exceeded.")
            else:
                if result_code.getName().lower() == "connection rate exceeded":
                    self.__logger.warning("Connection rate exceeded.")
        except Exception as e:
            self.__logger.exception("Error in on_connect callback: %s", exc_info=e)

        for callback in self.__service_subscription_callbacks:
            callback()

    def _on_disconnect(self, client, userdata, disconnect_flags, result_code, properties=None):
        # pylint: disable=protected-access
        self.__is_connected = False
        if self.client._client != client: # noqa pylint: disable=protected-access
            self.__logger.info("TB client %s has been disconnected. Current client for connection is: %s", str(client),
                               str(self.client._client)) # noqa pylint: disable=protected-access
            client.loop_stop()
        else:
            self.__is_connected = False
            if len(inspect.signature(self.client._on_disconnect).parameters) == 5: # noqa pylint: disable=protected-access
                self.client._on_disconnect(client, userdata, disconnect_flags, result_code, properties) # noqa pylint: disable=protected-access
            elif len(inspect.signature(self.client._on_disconnect).parameters) == 4: # noqa pylint: disable=protected-access
                self.client._on_disconnect(client, userdata, result_code, properties) # noqa pylint: disable=protected-access
            else:
                self.client._on_disconnect(client, userdata, result_code) # noqa pylint: disable=protected-access

    def stop(self):
        # self.disconnect()
        self.client.stop()
        self.__stopped = True
        self.__stop_event.set()

    def disconnect(self):
        self.__paused = True
        self.unsubscribe('*')
        return self.client.disconnect()

    def unsubscribe(self, subscription_id):
        self.client.gw_unsubscribe(subscription_id)
        self.client.unsubscribe_from_attribute(subscription_id)

    def connect(self, min_reconnect_delay=5, timeout=None):
        self.__paused = False
        self.__stopped = False
        self.__min_reconnect_delay = max(min_reconnect_delay, 2)

        start_connect_time = time()
        previous_connection_time = time()
        first_connection = True

        try:
            while not self.client.is_connected() and not self.__stopped:
                if not self.__paused:
                    if self.__stopped:
                        break
                    if timeout is not None and time() - start_connect_time > timeout:
                        self.__logger.error("Connection timeout.")
                        break
                    if self.client._client.is_connected():
                        self.__logger.info("Client connected to platform.")
                        if self.client.rate_limits_received:
                            self.__logger.info("Rate limits received.")
                        else:
                            self.__logger.info("Waiting for rate limits configuration...")
                    else:
                        self.__logger.info("Connecting to ThingsBoard...")
                    try:
                        if first_connection or time() - previous_connection_time > min_reconnect_delay:
                            first_connection = False
                            self.__send_connect()
                            previous_connection_time = time()
                        else:
                            sleep(1)
                    except ConnectionRefusedError:
                        self.__logger.error("Connection refused.")
                    except ConnectionError as e:
                        self.__logger.error("Connection error, cannot connect with error: %s", e)
                    except (ssl.SSLEOFError, ssl.SSLZeroReturnError) as e:
                        self.__logger.warning("Cannot use TLS connection on this port. "
                                              "Client will try to connect without TLS.")
                        self.__logger.debug("Error: %s", e)
                        self.client._client._ssl = False # noqa pylint: disable=protected-access
                        try:
                            self.__send_connect()
                        except Exception:
                            continue
                    except Exception as e:
                        self.__logger.error("Error in connect: %s", exc_info=e)
                sleep(1)
        except Exception as e:
            self.__logger.exception(e)
            sleep(10)

    def __send_connect(self):
        self.__logger.info(f"Sending connect to {self.__host}:{self.__port}")
        try:
            self.client.connect(keepalive=self.__config.get("keep_alive", 120),
                                min_reconnect_delay=self.__min_reconnect_delay)
            self.__logger.debug("Connect msg sent to platform.")
        except socket.gaierror as e:
            raise ConnectionError(e) from e

    def run(self):
        while not self.__stopped:
            try:
                if self.__stop_event.wait(timeout=0.1):
                    break
            except KeyboardInterrupt:
                self.__stopped = True
                self.__stop_event.set()
            except TimeoutError:
                sleep(0.1)
            except Exception as e:
                self.__logger.exception("Error in main MQTT client loop: %s", exc_info=e)

    def get_config_folder_path(self):
        return self.__config_folder_path

    def register_service_subscription_callback(self, subscribe_to_required_topics):
        self.__service_subscription_callbacks.append(subscribe_to_required_topics)

    @property
    def config(self):
        return self.__config

    def is_stopped(self):
        return self.__stopped

    def get_max_payload_size(self):
        return self.client.max_payload_size # noqa pylint: disable=protected-access

    def update_logger(self):
        self.__logger.setLevel(getLogger("tb_connection").level)
        self.__logger.handlers = getLogger("tb_connection").handlers
        self.__logger.manager = getLogger("tb_connection").manager
        self.__logger.disabled = getLogger("tb_connection").disabled
        self.__logger.filters = getLogger("tb_connection").filters
        self.__logger.propagate = getLogger("tb_connection").propagate
        self.__logger.parent = getLogger("tb_connection").parent
        tb_device_mqtt.log = self.__logger

    def get_rate_limits(self):
        return {
            'devices_connected_through_gateway_telemetry_messages_rate_limit': deepcopy(self.client._devices_connected_through_gateway_telemetry_messages_rate_limit.__dict__),  # noqa
            'devices_connected_through_gateway_telemetry_datapoints_rate_limit': deepcopy(self.client._devices_connected_through_gateway_telemetry_datapoints_rate_limit.__dict__),  # noqa
            'devices_connected_through_gateway_messages_rate_limit': deepcopy(self.client._devices_connected_through_gateway_messages_rate_limit.__dict__),  # noqa
            'messages_rate_limit': deepcopy(self.client._messages_rate_limit.__dict__),  # noqa
            'telemetry_rate_limit': deepcopy(self.client._telemetry_rate_limit.__dict__),  # noqa
            'telemetry_dp_rate_limit': deepcopy(self.client._telemetry_dp_rate_limit.__dict__),  # noqa
            'max_inflight_messages': self.client._client._max_inflight_messages,  # noqa
            'max_payload_size': self.client.max_payload_size
        }

    def is_subscribed_to_service_attributes(self):
        return GATEWAY_ATTRIBUTES_RESPONSE_TOPIC in self.client._gw_subscriptions.values() and GATEWAY_ATTRIBUTES_TOPIC in self.client._gw_subscriptions.values() # noqa pylint: disable=protected-access

    def update_client_rate_limits_with_previous(self, previous_limits):
        self.client._devices_connected_through_gateway_telemetry_messages_rate_limit = tb_device_mqtt.RateLimit(
            previous_limits.get('devices_connected_through_gateway_telemetry_messages_rate_limit', {}))
        self.client._devices_connected_through_gateway_telemetry_datapoints_rate_limit = tb_device_mqtt.RateLimit(
            previous_limits.get('devices_connected_through_gateway_telemetry_datapoints_rate_limit', {}))
        self.client._devices_connected_through_gateway_messages_rate_limit = tb_device_mqtt.RateLimit(
            previous_limits.get('devices_connected_through_gateway_messages_rate_limit', {}))

        self.client._messages_rate_limit = tb_device_mqtt.RateLimit(previous_limits.get('messages_rate_limit', {})) # noqa pylint: disable=protected-access
        self.client._telemetry_rate_limit = tb_device_mqtt.RateLimit(previous_limits.get('telemetry_rate_limit', {})) # noqa pylint: disable=protected-access
        self.client._telemetry_dp_rate_limit = tb_device_mqtt.RateLimit(previous_limits.get('telemetry_dp_rate_limit', {})) # noqa pylint: disable=protected-access
        self.client.max_inflight_messages_set(previous_limits.get('max_inflight_messages', 20)) # noqa pylint: disable=protected-access
        self.client.max_payload_size = previous_limits.get('max_payload_size', 8196) # noqa pylint: disable=protected-access
