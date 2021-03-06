import logging
import socket
try:
    import socketserver
except ImportError:
    import SocketServer as socketserver
import threading
import select
import can
from can.interfaces.remote import events


logger = logging.getLogger(__name__)


class RemoteServer(socketserver.ThreadingTCPServer):
    """Server for CAN communication."""

    def __init__(self, host='0.0.0.0', port=None, **config):
        """
        :param str host:
            Address to listen to.
        :param int port:
            Network port to listen to.
        :param channel:
            The can interface identifier. Expected type is backend dependent.
        :param str bustype:
            CAN interface to use.
        :param int bitrate:
            Forced bitrate in bits/s.
        """
        address = (host, port or can.interfaces.remote.DEFAULT_PORT)
        self.config = config
        #: List of :class:`can.interfaces.remote.server.ClientBusConnection`
        #: instances
        self.clients = []
        socketserver.TCPServer.__init__(self, address, ClientBusConnection)
        logger.info("Server listening on %s:%d", address[0], address[1])


class ClientBusConnection(socketserver.BaseRequestHandler):
    """A client connection on the server."""

    def setup(self):
        # Disable Nagle algorithm for better real-time performance
        self.request.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

        self.config = dict(self.server.config)
        self.bus = None
        self.conn = can.interfaces.remote.connection.Connection()
        # Threads will finish up when this is set
        self.stop_event = threading.Event()
        self.send_thread = threading.Thread(target=self._send_to_client,
                                            name='Send to client')
        self.send_thread.daemon = True
        self.send_tasks = {}
        # Register with the server
        self.server.clients.append(self)

    def handle(self):
        bus_event = self._next_event()
        if not isinstance(bus_event, events.BusRequest):
            raise RemoteServerError('Handshake error')

        if bus_event.version != can.interfaces.remote.PROTOCOL_VERSION:
            raise RemoteServerError('Protocol version mismatch (%d != %d)' % (
                bus_event.version, can.interfaces.remote.PROTOCOL_VERSION))

        self.config.setdefault("bitrate", bus_event.bitrate)

        filter_event = self._next_event()
        if not isinstance(filter_event, events.FilterConfig):
            raise RemoteServerError('Handshake error')
        self.config["can_filters"] = filter_event.can_filters

        try:
            self.bus = can.interface.Bus(**self.config)
        except Exception as e:
            self.conn.send_event(events.RemoteException(e))
            raise
        else:
            logger.info("Connected to bus '%s'", self.bus.channel_info)
            self.conn.send_event(events.BusResponse(self.bus.channel_info))
            # Register with the server
            self.server.clients.append(self)
        finally:
            self.request.sendall(self.conn.next_data())

        self.send_thread.start()
        self._receive_from_client()

    def finish(self):
        logger.info("Closing connection to %s", self.request.getpeername())
        # Remove itself from the server's list of clients
        self.server.clients.remove(self)
        self.stop_event.set()
        if self.send_thread.is_alive():
            self.send_thread.join(3)

    def _next_event(self):
        """Block until a new event has been received.

        :return: Next event in queue
        """
        event = self.conn.next_event()
        while event is None:
            self.conn.receive_data(self.request.recv(256))
            event = self.conn.next_event()
        return event

    def _next_event_from_bus(self, timeout=None):
        """Block until a new a CAN message is received or an exception occurrs.

        :return: An event
        """
        try:
            msg = self.bus.recv(timeout)
        except can.CanError as e:
            logger.error(e)
            return events.RemoteException(e)
        else:
            return None if msg is None else events.CanMessage(msg)

    def _receive_from_client(self):
        """Continuously read events from socket and send messages on CAN bus."""
        while not self.stop_event.is_set():
            event = self._next_event()
            if isinstance(event, events.CanMessage):
                self.send_msg(event.msg)
            elif isinstance(event, events.ConnectionClosed):
                break
            elif isinstance(event, events.PeriodicMessageStart):
                if event.msg.arbitration_id in self.send_tasks:
                    # Modify already existing task
                    self.send_tasks[event.msg.arbitration_id].modify_data(event.msg)
                else:
                    # Create new task
                    task = self.bus.send_periodic(event.msg,
                                                  event.period,
                                                  event.duration)
                    self.send_tasks[event.msg.arbitration_id] = task
            elif isinstance(event, events.PeriodicMessageStop):
                self.send_tasks[event.arbitration_id].stop()

    def _send_to_client(self):
        """Continuously read CAN messages and send to client."""
        while not self.stop_event.is_set():
            # Wait for an event on CAN (max 0.5 seconds)
            event = self._next_event_from_bus(0.5)

            # Wait for client to be ready for new messages (max 2 seconds)
            client_ready = len(select.select([], [self.request], [], 2)[1]) > 0

            # Read all CAN events to buffer
            while event is not None:
                self.conn.send_event(event)
                if isinstance(event, events.RemoteException):
                    # An exception while reading from CAN is probably a serious
                    # error so we should stop everything
                    self.stop_event.set()
                    break
                event = self._next_event_from_bus(0)

            # Send all data at once if there is any
            if self.conn.data_ready() and client_ready:
                self.request.sendall(self.conn.next_data())

        logger.info('Disconnecting from CAN bus')
        self.bus.shutdown()

    def send_msg(self, msg):
        """Send a CAN message to the bus."""
        try:
            self.bus.send(msg)
        except can.CanError as e:
            logger.error(str(e))
            self.conn.send_event(events.TransmitFail())


class RemoteServerError(Exception):
    pass
