import base64
import datetime
import logging
import os
import pprint
import threading
import time

from .backend import ArloBackEnd
from .background import ArloBackground
from .base import ArloBase
from .camera import ArloCamera
from .cfg import ArloCfg
from .constant import (BLANK_IMAGE, DEVICES_PATH,
                       FAST_REFRESH_INTERVAL, SLOW_REFRESH_INTERVAL,
                       TOTAL_BELLS_KEY, TOTAL_CAMERAS_KEY, TOTAL_LIGHTS_KEY, MEDIA_LIBRARY_DELAY,
                       REFRESH_CAMERA_DELAY, INITIAL_REFRESH_DELAY)
from .doorbell import ArloDoorBell
from .light import ArloLight
from .media import ArloMediaLibrary
from .storage import ArloStorage
from .util import time_to_arlotime

_LOGGER = logging.getLogger('pyaarlo')

__version__ = '0.6.17'


class PyArlo(object):
    """ Entry point for all Arlo operations.

    This is used to login to Arlo, open and maintain an evenstream with Arlo, find and store devices and device
    state, provide keep-alive services and make sure media sources are kept up to date.

    Every device discovered and created is done in here, every device discovered and created uses this instance
    to log errors, info and debug, to access the state database and configuration settings.

    **Required parameters:**

    * **username** - Your Arlo username.
    * **password** - Your Arlo password.

    **Optional parameters:**

    * **wait_for_initial_setup** - Wait for initial devices states to load before returning from constructor.
      Default `True`. Setting to `False` and using saved state can increase startup time.
    * **last_format** - Date string format used when showing video file dates. Default ``%m-%d %H:%M``.
    * **library_days** - Number of days of recordings to load. Default is `30`. If you have a lot of recordings
      you can lower this value.
    * **save_state** - Store device state across restarts. Default `True`.
    * **state_file** - Where to store state. Default is `${storage_dir}/${name.}pickle`
    * **refresh_devices_every** - Time, in hours, to refresh the device list from Arlo. This can help keep the login
      from timing out.
    * **stream_timeout** - Time, in seconds, for the event stream to close after receiving no packets. 0 means no timeout.
      Default 0 seconds. Setting this to `120` can be useful for catching dead connections - ie, an ISP forced
      a new IP on you.

    **Debug parameters:**

    * **dump** - Save event stream packets to a file.
    * **dump_file** - Where to packets. Default is `${storage_dir}/packets.dump`
    * **name** - Name used for state and dump files.
    * **verbose_debug** - If `True`, provide extra debug in the logs. This includes packets in and out.

    **2FA authentication parameters:**

    These parameters are needed for 2FA.

    * **tfa_source** - Where to get the token from. Default is `console`. Can be `imap` to use email.
    * **tfa_type** - How to get the 2FA token delivered. Default is `email` but can be `sms`.
    * **tfa_timeout** - When using `imap`, how long, in seconds, to wait between checks.
    * **tfa_total_timeout** - When using `imap`, how long, in seconds, for all checks.
    * **imap_host** - When using `imap`, host name of imap server.
    * **imap_username** - When using `imap`, user name on imap server. If `None` will use Arlo username.
    * **imap_password** - When using `imap`, password on imap server. If `None` will use Arlo password.

    **Infrequently used parameters:**

    These parameters are very rarely changed.

    * **host** - Arlo host to use. Default `https://my.arlo.com`.
    * **storage_dir** - Where to store saved state.
    * **db_motion_time** - Time, in seconds, to show active for doorbell motion detected. Default 30 seconds.
    * **db_ding_time** - Time, in seconds, to show on for doorbell button press. Default 10 seconds.
    * **request_timeout** - Time, in seconds, for requests sent to Arlo to succeed. Default 60 seconds.
    * **recent_time** - Time, in seconds, for the camera to indicate it has seen motion. Default 600 seconds.
    * **no_media_upload** - Force a media upload after camera activity.
      Normally not needed but some systems fail to push media uploads. Default 'False'.
    * **user_agent** - Set what 'user-agent' string is passed in request headers. It affects what video stream type is
      returned. Default is `apple`.
    * **mode_api** - Which api to use to set the base station modes. Default is `auto` which choose an API based on camera
      model. Can also be `v1` and `v2`.
    * **http_connections** - HTTP connection pool size. Default is `20`, set to `None` to default provided by the system.
    * **http_max_size** - HTTP maximum connection pool size. Default is `10`, set to `None` to default provided by the system.
    * **reconnect_every** - Time, in minutes, to close and relogin to Arlo.
    * **snapshot_timeout** - Time, in seconds, to stop the snapshot attempt and return the camera to the idle state.

    """

    def __init__(self, **kwargs):
        """ Constructor for the PyArlo object.
        """
        # core values
        self._last_error = None

        # Set up the config first.
        self._cfg = ArloCfg(self, **kwargs)

        # Create storage/scratch directory.
        if self._cfg.state_file is not None or self._cfg.dump_file is not None:
            try:
                os.mkdir(self._cfg.storage_dir)
            except Exception:
                pass

        # Create remaining components.
        self._bg = ArloBackground(self)
        self._st = ArloStorage(self)
        self._be = ArloBackEnd(self)
        self._ml = ArloMediaLibrary(self)

        # Failed to login, then stop now!
        if not self._be.is_connected:
            return

        self._lock = threading.Condition()
        self._bases = []
        self._cameras = []
        self._lights = []
        self._doorbells = []

        # On day flip we do extra work, record today.
        self._today = datetime.date.today()

        # Every few hours we can refresh the device list.
        self._refresh_devices_at = time.monotonic() + self._cfg.refresh_devices_every

        # default blank image when waiting for camera image to appear
        self._blank_image = base64.standard_b64decode(BLANK_IMAGE)

        # Slow piece.
        # Get devices, fill local db, and create device instance.
        self.info('pyaarlo starting')
        self._started = False
        self._refresh_devices()
        for device in self._devices:
            dname = device.get('deviceName')
            dtype = device.get('deviceType')
            if device.get('state', 'unknown') != 'provisioned':
                self.info('skipping ' + dname + ': state unknown')
                continue

            if dtype == 'basestation' or \
                    device.get('modelId') == 'ABC1000' or dtype == 'arloq' or dtype == 'arloqs' or \
                    device.get('modelId').startswith('AVD1001'):
                self._bases.append(ArloBase(dname, self, device))
            if dtype == 'arlobridge':
                self._bases.append(ArloBase(dname, self, device))
            if dtype == 'camera' or dtype == 'arloq' or dtype == 'arloqs' or \
                    device.get('modelId').startswith('AVD1001'):
                self._cameras.append(ArloCamera(dname, self, device))
            if dtype == 'doorbell':
                self._doorbells.append(ArloDoorBell(dname, self, device))
            if dtype == 'lights':
                self._lights.append(ArloLight(dname, self, device))

        # Save out unchanging stats!
        self._st.set(['ARLO', TOTAL_CAMERAS_KEY], len(self._cameras))
        self._st.set(['ARLO', TOTAL_BELLS_KEY], len(self._doorbells))
        self._st.set(['ARLO', TOTAL_LIGHTS_KEY], len(self._lights))

        # Always ping bases first!
        self._ping_bases()

        # Queue up initial config and state retrieval.
        self.debug('getting initial settings')
        self._bg.run_in(self._refresh_camera_thumbnails, REFRESH_CAMERA_DELAY)
        self._bg.run_in(self._refresh_camera_media, REFRESH_CAMERA_DELAY)
        self._bg.run_in(self._initial_refresh, INITIAL_REFRESH_DELAY)
        self._bg.run_in(self._ml.load, MEDIA_LIBRARY_DELAY)

        # Register house keeping cron jobs.
        self.debug('registering cron jobs')
        self._bg.run_every(self._fast_refresh, FAST_REFRESH_INTERVAL)
        self._bg.run_every(self._slow_refresh, SLOW_REFRESH_INTERVAL)

        # Wait for initial refresh
        if self._cfg.wait_for_initial_setup:
            with self._lock:
                while not self._started:
                    self.debug('waiting for initial setup...')
                    self._lock.wait(5)
            self.debug('finished...')

    def __repr__(self):
        # Representation string of object.
        return "<{0}: {1}>".format(self.__class__.__name__, self._cfg.name)

    def _refresh_devices(self):
        self._devices = self._be.get(DEVICES_PATH + "?t={}".format(time_to_arlotime()))
        self.vdebug("devices={}".format(pprint.pformat(self._devices)))

    def _refresh_camera_thumbnails(self):
        """ Request latest camera thumbnails, called at start up. """
        for camera in self._cameras:
            camera.update_last_image()

    def _refresh_camera_media(self):
        """ Rebuild cameras media library, called at start up or when day changes. """
        for camera in self._cameras:
            camera.update_media()

    def _refresh_ambient_sensors(self):
        for camera in self._cameras:
            camera.update_ambient_sensors()

    def _ping_bases(self):
        for base in self._bases:
            base.ping()

    def _refresh_bases(self, initial):
        for base in self._bases:
            base.update_modes()
            if initial:
                base.update_mode()
            self._be.notify(base=base, body={"action": "get", "resource": "cameras", "publishResponse": False})
            self._be.notify(base=base, body={"action": "get", "resource": "doorbells", "publishResponse": False})
            self._be.notify(base=base, body={"action": "get", "resource": "lights", "publishResponse": False})

    def _fast_refresh(self):
        self.debug('fast refresh')
        self._bg.run(self._st.save)
        self._ping_bases()

        # if day changes then reload recording library and camera counts
        today = datetime.date.today()
        self.debug('day testing with {}!'.format(str(today)))
        if self._today != today:
            self.debug('day changed to {}!'.format(str(today)))
            self._today = today
            self._bg.run(self._ml.load)
            self._bg.run(self._refresh_camera_media)

    def _slow_refresh(self):
        self.debug('slow refresh')
        self._bg.run(self._refresh_bases, initial=False)
        self._bg.run(self._refresh_ambient_sensors)

        # do we need to reload the devices?
        if self._cfg.refresh_devices_every != 0:
            now = time.monotonic()
            self.debug('device reload check {} {}'.format(str(now), str(self._refresh_devices_at)))
            if now > self._refresh_devices_at:
                self.debug('device reload needed')
                self._refresh_devices_at = now + self._cfg.refresh_devices_every
                self._bg.run(self._refresh_devices)
        else:
            self.debug('no device reload')

    def _initial_refresh(self):
        self.debug('initial refresh')
        self._bg.run(self._refresh_bases, initial=True)
        self._bg.run(self._refresh_ambient_sensors)
        self._bg.run(self._initial_refresh_done)

    def _initial_refresh_done(self):
        self.debug('initial refresh done')
        with self._lock:
            self._started = True
            self._lock.notify_all()

    def stop(self):
        """ Stop connection to Arlo and logout. """
        self._st.save()
        self._be.logout()

    @property
    def cfg(self):
        return self._cfg

    @property
    def bg(self):
        return self._bg

    @property
    def st(self):
        return self._st

    @property
    def be(self):
        return self._be

    @property
    def ml(self):
        return self._ml

    @property
    def is_connected(self):
        """ Is the object connected to the Arlo servers.

        :return: `True` if it's connected, `False` otherwise.
        :rtype: bool
        """
        return self._be.is_connected

    @property
    def cameras(self):
        """ List of registered cameras.

        :return: a list of cameras.
        :rtype: list(ArloCamera)
        """
        return self._cameras

    @property
    def doorbells(self):
        """ List of registered doorbells.

        :return: a list of doorbells.
        :rtype: list(ArloDoorBell)
        """
        return self._doorbells

    @property
    def lights(self):
        """ List of registered lights.

        :return: a list of lights.
        :rtype: list(ArloLight)
        """
        return self._lights

    @property
    def base_stations(self):
        """ List of base stations..

        :return: a list of base stations.
        :rtype: list(ArloBase)
        """
        return self._bases

    @property
    def blank_image(self):
        """ Return a binaryy representation of a blank image.

        :return: A bytes representation of a blank image.
        :rtype: bytearray
        """
        return self._blank_image

    def lookup_camera_by_id(self, device_id):
        """ Return the camera referenced by `device_id`.

        :param device_id: The camera device to look for
        :return: A camera object or 'None' on failure.
        :rtype: ArloCamera
        """
        camera = list(filter(lambda cam: cam.device_id == device_id, self.cameras))
        if camera:
            return camera[0]
        return None

    def lookup_camera_by_name(self, name):
        """ Return the camera called `name`.

        :param device_id: The camera name to look for
        :return: A camera object or 'None' on failure.
        :rtype: ArloCamera
        """
        camera = list(filter(lambda cam: cam.name == name, self.cameras))
        if camera:
            return camera[0]
        return None

    def lookup_doorbell_by_id(self, device_id):
        """ Return the doorbell referenced by `device_id`.

        :param device_id: The doorbell device to look for
        :return: A doorbell object or 'None' on failure.
        :rtype: ArloDoorBell
        """
        doorbell = list(filter(lambda cam: cam.device_id == device_id, self.doorbells))
        if doorbell:
            return doorbell[0]
        return None

    def lookup_doorbell_by_name(self, name):
        """ Return the doorbell called `name`.

        :param device_id: The doorbell name to look for
        :return: A doorbell object or 'None' on failure.
        :rtype: ArloDoorBell
        """
        doorbell = list(filter(lambda cam: cam.name == name, self.doorbells))
        if doorbell:
            return doorbell[0]
        return None

    def inject_response(self, response):
        """ Inject a test packet into the event stream.

        **Note:** The method makes no effort to check the packet.

        :param response: packet to inject.
        :type response: JSON data
        """
        self.debug("injecting\n{}".format(pprint.pformat(response)))
        self._be._ev_dispatcher(response)

    def attribute(self, attr):
        """ Return the value of attribute attr.

        PyArlo stores its state in key/value pairs. This returns the value associated with the key.

        :param attr: Attribute to look up.
        :type attr: str
        :return: The value associated with attribute or `None` if not found.
        :rtype: str
        """
        return self._st.get(['ARLO', attr], None)

    def add_attr_callback(self, attr, cb):
        pass

    # TODO needs thinking about... track new cameras for example.
    def update(self, update_cameras=False, update_base_station=False):
        pass

    def error(self, msg):
        self._last_error = msg
        _LOGGER.error(msg)

    @property
    def last_error(self):
        """ Return the last seen error.

        :return: The last error reported by the object or any device, 'None' if no errors.
        :rtype: str or None
        """
        return self._last_error

    def warning(self, msg):
        _LOGGER.warning(msg)

    def info(self, msg):
        _LOGGER.info(msg)

    def debug(self, msg):
        _LOGGER.debug(msg)

    def vdebug(self, msg):
        if self._cfg.verbose:
            _LOGGER.debug(msg)
