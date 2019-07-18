import ctypes
import enum
import functools
import logging
import queue
import struct
import threading
import time

from collections import OrderedDict

from qtpy.QtCore import Slot

from pydm.utilities.channel import parse_channel_config
from pydm.data_store import DataKeys
from pydm.data_plugins.plugin import PyDMPlugin, PyDMConnection


import pyads
from pyads import structs, constants


logger = logging.getLogger(__name__)


class ADST_Type(enum.IntEnum):
    VOID = 0
    INT8 = 16
    UINT8 = 17
    INT16 = 2
    UINT16 = 18
    INT32 = 3
    UINT32 = 19
    INT64 = 20
    UINT64 = 21
    REAL32 = 4
    REAL64 = 5
    BIGTYPE = 65
    STRING = 30
    WSTRING = 31
    REAL80 = 32
    BIT = 33
    MAXTYPES = 34


ads_type_to_ctype = {
    # ADST_VOID
    ADST_Type.INT8: constants.PLCTYPE_BYTE,
    ADST_Type.UINT8: constants.PLCTYPE_UBYTE,
    ADST_Type.INT16: constants.PLCTYPE_INT,
    ADST_Type.UINT16: constants.PLCTYPE_UINT,
    ADST_Type.INT32: constants.PLCTYPE_DINT,
    ADST_Type.UINT32: constants.PLCTYPE_UDINT,
    ADST_Type.INT64: constants.PLCTYPE_LINT,
    ADST_Type.UINT64: constants.PLCTYPE_ULINT,
    ADST_Type.REAL32: constants.PLCTYPE_REAL,
    ADST_Type.REAL64: constants.PLCTYPE_LREAL,
    # ADST_BIGTYPE
    ADST_Type.STRING: constants.PLCTYPE_STRING,
    # ADST_WSTRING
    # ADST_REAL80
    ADST_Type.BIT: constants.PLCTYPE_BOOL,
}


def parse_address(addr):
    'ads://<host>[:<port>][/@poll_rate]/<symbol>'
    host_info, _, symbol = addr.partition('/')

    if ':' in host_info:
        host, port = host_info.split(':')
    else:
        host, port = host_info, 851

    if '@' in host:
        ams_id, ip_address = host.split('@')
    elif host.count('.') == 3:
        ip_address = host
        ams_id = '{}.1.1'.format(ip_address)
    elif host.count('.') == 5:
        ams_id = host
        if not ams_id.endswith('.1.1'):
            raise ValueError('Cannot assume IP address without an AMS ID '
                             'that ends with .1.1')
        ip_address = ams_id[:-4]
    else:
        raise ValueError(f'Cannot parse host string: {host!r}')

    if symbol.startswith('@') and '/' in symbol:
        poll_info, _, symbol = symbol.partition('/')
        poll_rate = poll_info.lstrip('@')
    else:
        poll_rate = None

    return {'ip_address': ip_address,
            'host': host,
            'ams_id': ams_id,
            'port': int(port),
            'poll_rate': float(poll_rate) if poll_rate is not None else None,
            'symbol': symbol,
            'use_notify': True,
            }


def get_symbol_information(plc, symbol_name) -> structs.SAdsSymbolEntry:
    return plc.read_write(
        constants.ADSIGRP_SYM_INFOBYNAMEEX,
        0x0,
        structs.SAdsSymbolEntry,
        symbol_name,
        constants.PLCTYPE_STRING,
    )


def unpack_notification(notification, plc_datatype):
    contents = notification.contents
    data_size = contents.cbSampleSize
    # Get dynamically sized data array
    data = (ctypes.c_ubyte * data_size).from_address(
        ctypes.addressof(contents) +
        structs.SAdsNotificationHeader.data.offset)

    datatype_map = {
        constants.PLCTYPE_BOOL: "<?",
        constants.PLCTYPE_BYTE: "<c",
        constants.PLCTYPE_DINT: "<i",
        constants.PLCTYPE_DWORD: "<I",
        constants.PLCTYPE_INT: "<h",
        constants.PLCTYPE_LREAL: "<d",
        constants.PLCTYPE_REAL: "<f",
        constants.PLCTYPE_SINT: "<b",
        constants.PLCTYPE_UDINT: "<L",
        constants.PLCTYPE_UINT: "<H",
        constants.PLCTYPE_USINT: "<B",
        constants.PLCTYPE_WORD: "<H",
    }

    if plc_datatype == constants.PLCTYPE_STRING:
        # read only until null-termination character
        value = bytearray(data).split(b"\0", 1)[0].decode("utf-8")

    elif issubclass(plc_datatype, ctypes.Structure):
        value = plc_datatype()
        fit_size = min(data_size, ctypes.sizeof(value))
        ctypes.memmove(ctypes.addressof(value), ctypes.addressof(data),
                       fit_size)
    elif plc_datatype not in datatype_map:
        value = bytearray(data)
    else:
        value, = struct.unpack(datatype_map[plc_datatype], bytearray(data))

    timestamp = pyads.filetimes.filetime_to_dt(contents.nTimeStamp)
    return timestamp, value


def get_symbol_data_type(plc, symbol_name, *, custom_types=None):
    info = get_symbol_information(plc, symbol_name)
    type_name = info.type_name
    data_type_int = info.dataType

    if custom_types is None:
        custom_types = {}

    if data_type_int in custom_types:
        data_type = custom_types[data_type_int]
    elif type_name in custom_types:
        # Potential feature: allow mapping of type names to structures by
        # registering them in `custom_types`
        data_type = custom_types[type_name]
    elif data_type_int in ads_type_to_ctype:
        data_type = ads_type_to_ctype[data_type_int]
    elif type_name in ads_type_to_ctype:
        # Potential feature: allow mapping of type names to structures by
        # registering them in `ads_type_to_ctype`
        data_type = ads_type_to_ctype[type_name]
    else:
        raise ValueError(
            'Unsupported data type {!r} (number={} size={} comment={!r})'
            ''.format(type_name, data_type_int,
                      info.size, info.comment)
        )

    if data_type is constants.PLCTYPE_STRING:
        array_length = 1
    else:
        # String types are handled directly by adsSyncReadReqEx2.
        # Otherwise, if the reported size is larger than the data type
        # size, it is an array of that type:
        array_length = info.size // ctypes.sizeof(data_type)
        if array_length > 1:
            data_type = data_type * array_length

    return data_type, array_length


def enumerate_plc_symbols(plc):
    symbol_info = plc.read(constants.ADSIGRP_SYM_UPLOADINFO, 0x0,
                           structs.SAdsSymbolUploadInfo)

    symbol_buffer = bytearray(
        plc.read(constants.ADSIGRP_SYM_UPLOAD, 0,
                 ctypes.c_ubyte * symbol_info.nSymSize,
                 return_ctypes=True))

    symbol_buffer = bytearray(symbol_buffer)

    symbols = {}
    while symbol_buffer:
        if len(symbol_buffer) < ctypes.sizeof(structs.SAdsSymbolEntry):
            symbol_buffer += (bytearray(ctypes.sizeof(structs.SAdsSymbolEntry)
                                        - len(symbol_buffer)))
        entry = structs.SAdsSymbolEntry.from_buffer(symbol_buffer)
        if entry.entryLength == 0:
            break

        symbols[entry.name] = {'entry': entry,
                               'type': entry.type_name,
                               'comment': entry.comment}
        symbol_buffer = symbol_buffer[entry.entryLength:]

    return symbols


class Symbol:
    def __init__(self, plc, symbol, poll_rate):
        self.plc = plc
        self.symbol = symbol
        self.connection = None
        self.ads = self.plc.ads
        self.data_type = None
        self.array_size = None
        self.conn = None
        self.notification_handle = None
        self.poll_rate = poll_rate
        self.data = {DataKeys.CONNECTION: False}

    def _notification_update(self, notification, name):
        timestamp, value = unpack_notification(notification, self.data_type)
        self.send_to_channel(timestamp, value)

    def send_to_channel(self, timestamp, value):
        self.data.update(**{
            DataKeys.VALUE: value,
            # DataKeys.TIMESTAMP: time.time(),
        })
        self.conn.send_new_value(self.data)

    def _update_data_type(self):
        self.data_type, self.array_size = get_symbol_data_type(
            self.ads, self.symbol)
        self.data[DataKeys.CONNECTION] = True

    def poll(self):
        if self.data_type is None:
            self._update_data_type()
        value = self.ads.read_by_name(self.symbol, plc_datatype=self.data_type)
        self.send_to_channel(time.time(), value)

    def set_connection(self, conn):
        def init():
            if self.poll_rate is None:
                attr = pyads.NotificationAttrib(ctypes.sizeof(self.data_type))
                self.notification_handle = self.ads.add_device_notification(
                    self.symbol, attr, self._notification_update)
            else:
                self.poll()

        self.conn = conn
        self.plc.add_to_queue(init)
        if self.poll_rate is not None:
            self.plc.add_to_poll_thread(self.poll_rate, self.poll)


class Plc:
    def __init__(self, ip_address, ams_id, port):
        self.running = True
        self.ip_address = ip_address
        self.ams_id = ams_id
        self.port = port
        self.symbols = {}
        self.ads = pyads.Connection(ams_id, port, ip_address=ip_address)
        self.queue = queue.Queue()
        self.thread = threading.Thread(target=self._thread, daemon=True)
        self.thread.start()
        self.poll_threads = {}

    def add_to_poll_thread(self, rate, func, *args, **kwargs):
        if rate not in self.poll_threads:
            thread = threading.Thread(target=self._poll_thread, args=(rate, ),
                                      daemon=True)
            self.poll_threads[rate] = dict(thread=thread, calls=[])
            thread.start()

        self.poll_threads[rate]['calls'].append((func, args, kwargs))

    def stop(self):
        self.running = False
        self.add_to_queue(lambda: None)

    def add_to_queue(self, func, *args, **kwargs):
        self.queue.put((func, args, kwargs))

    def _poll_thread(self, rate):
        while self.running:
            info = self.poll_threads[rate]
            t0 = time.time()
            for func, args, kwargs in list(info['calls']):
                try:
                    func(*args, **kwargs)
                except Exception:
                    logger.exception(
                        'Poll thread %s:%s:%d @ %.3f sec failure: '
                        '%s(*%r, **%r)',
                        self.ip_address, self.ams_id, self.port,
                        rate, func.__name__, args, kwargs
                    )
                    info['calls'].remove(func)
            elapsed = time.time() - t0
            time.sleep(max((0, rate - elapsed)))

    def _thread(self):
        while self.running:
            func, args, kwargs = self.queue.get()
            try:
                func(*args, **kwargs)
            except Exception:
                logger.exception('PLC thread %s:%s:%d failure: %s(*%r, **%r)',
                                 self.ip_address, self.ams_id, self.port,
                                 func.__name__, args, kwargs)
        self.ads.close()

    def clear_symbol(self, symbol):
        _ = self.symbols.pop(symbol)
        if not self.symbols:
            self.ads.close()

    def get_symbol(self, symbol_name, poll_rate):
        key = (symbol_name, poll_rate)
        try:
            return self.symbols[key]
        except KeyError:
            if not self.ads.is_open:
                self.ads.open()
            self.symbols[key] = Symbol(self, symbol_name, poll_rate)
            return self.symbols[key]


_PLCS = {}


def get_connection(ip_address, ams_id, port):
    key = (ip_address, ams_id, port)
    try:
        return _PLCS[key]
    except KeyError:
        plc = Plc(ip_address, ams_id, port)
        _PLCS[key] = plc
        return plc


class Connection(PyDMConnection):
    def __init__(self, channel, address, protocol=None, parent=None):
        super().__init__(channel, address, protocol, parent)
        conn = parse_channel_config(address, force_dict=True)['connection']
        address = conn.get('parameters', {}).get('address')

        self.address = parse_address(address)
        self.ip_address = self.address['ip_address']
        self.ams_id = self.address['ams_id']
        self.port = self.address['port']
        self.poll_rate = self.address['poll_rate']
        self.plc = get_connection(ip_address=self.ip_address,
                                  ams_id=self.ams_id, port=self.port)

        self.symbol_name = self.address['symbol']
        self.symbol = self.plc.get_symbol(self.symbol_name, self.poll_rate)
        self.symbol.set_connection(self)

    def send_new_value(self, payload):
        self.data.update(payload)
        self.send_to_channel()

    @Slot(dict)
    def receive_from_channel(self, payload):
        ...

    def close(self):
        print('connection closed', self.symbol_name)
        self.plc.clear_symbol(self.symbol_name)
        super().close()


class ADSPlugin(PyDMPlugin):
    protocol = 'ads'
    connection_class = Connection
