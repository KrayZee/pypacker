"""
Packet interceptor using NFQueue

Requirements:
- CPython
- NFQUEUE target support in Kernel
- iptables
"""
import ctypes
from ctypes import util as utils
import socket
from socket import htons, ntohl
from socket import timeout as socket_timeout
import threading
from collections import namedtuple
import logging

logger = logging.getLogger("pypacker")

MSG_NO_NFQUEUE = """Could not find netfilter_queue library. Make sure that...
- libnetfilter_queue is installed
- NFQUEUE target is supported by your Kernel:
	Networking Options
		Network packet filtering ...
			Core Netfilter ...
				NFQUEUE target
- iptables is installed and callable via "iptables"
- NFQUEUE related rulez can be added eg "iptables -I INPUT 1 -j NFQUEUE --queue-num 0"
"""

netfilter = None

try:
	# load library
	nflib = utils.find_library("netfilter_queue")

	if nflib is None:
		raise RuntimeError()

	netfilter = ctypes.cdll.LoadLibrary(nflib)
except RuntimeError:
	logger.warning(MSG_NO_NFQUEUE)


class NfqQHandler(ctypes.Structure):
	pass


class NfnlHandle(ctypes.Structure):
	pass


nfnl_callback_ctype = ctypes.CFUNCTYPE(
	ctypes.c_int, *(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)
)


class NfnlCallback(ctypes.Structure):
	_fileds_ = [("call", nfnl_callback_ctype),
		("data", ctypes.c_void_p),
		("attr_count", ctypes.c_uint16)
	]


class NfnlSubsysHandle(ctypes.Structure):
	_fields_ = [("nfilter_handler", ctypes.POINTER(NfnlHandle)),
		("subscriptions", ctypes.c_uint32),
		("subsys_id", ctypes.c_uint8),
		("cb_count", ctypes.c_uint8),
		("callback", ctypes.POINTER(NfnlCallback))
	]


class NfqHandle(ctypes.Structure):
	_fields_ = [("nfnlh", ctypes.POINTER(NfnlHandle)),
		("nfnlssh", ctypes.POINTER(NfnlSubsysHandle)),
		("qh_list", ctypes.POINTER(NfqQHandler))
	]


class NfqQHandle(ctypes.Structure):
	_fields_ = [("next", ctypes.POINTER(NfqQHandler)),
		("h", ctypes.POINTER(NfqHandle)),
		("id", ctypes.c_uint16),
		("cb", ctypes.POINTER(NfnlHandle)),
		("data", ctypes.c_void_p)
	]


class NfqData(ctypes.Structure):
	_fields_ = [("data", ctypes.POINTER(ctypes.c_void_p))]


class NfqnlMsgPacketHw(ctypes.Structure):
	_fields_ = [("hw_addrlen", ctypes.c_uint16),
		("_pad", ctypes.c_uint16),
		#############################
		("hw_addr", ctypes.c_uint8 * 8)]


class NfqnlMsgPacketHdr(ctypes.Structure):
	_fields_ = [("packet_id", ctypes.c_uint32),
		("hw_protocol", ctypes.c_uint16),
		("hook", ctypes.c_uint8)
	]


class NfnlHandler(ctypes.Structure):
	_fields_ = [("fd", ctypes.c_int),
		("subscriptions", ctypes.c_uint32),
		("seq", ctypes.c_uint32),
		("dump", ctypes.c_uint32),
		("rcv_buffer_size", ctypes.c_uint32),
		#####################################
		("local", ctypes.c_void_p),
		("peer", ctypes.c_void_p),
		("last_nlhdr", ctypes.c_void_p),
		("subsys", ctypes.c_void_p)
	]


class NlifHandle(ctypes.Structure):
	_fields_ = [("ifindex_max", ctypes.c_void_p),
		("rtnl_handle", ctypes.c_void_p),
		("ifadd_handler", ctypes.c_void_p),
		("ifdel_handler", ctypes.c_void_p)
	]


class Timeval(ctypes.Structure):
	_fields_ = [("tv_sec", ctypes.c_long),
		("tv_usec", ctypes.c_long)]


class PacketPool(ctypes.Structure):
	_fields_ = [("tv_sec", ctypes.c_long),
				("tv_usec", ctypes.c_long)]


# Return netfilter netlink handler
nfnlh = netfilter.nfq_nfnlh
nfnlh.restype = ctypes.POINTER(NfnlHandle)
nfnlh.argtypes = ctypes.POINTER(NfqHandle),

# Return a file descriptor for the netlink connection associated with the
# given queue connection handle.
nfq_fd = netfilter.nfnl_fd
nfq_fd.restype = ctypes.c_int
nfq_fd.argtypes = ctypes.POINTER(NfnlHandle),

# This function obtains a netfilter queue connection handle
ll_open_queue = netfilter.nfq_open
ll_open_queue.restype = ctypes.POINTER(NfqHandle)

# Open a nfqueue handler from a existing nfnetlink handler.
# Not implemented in this wrapper.
#open_nfnl = netfilter.nfq_open_nfnl
#open_nfnl.restype = ctypes.POINTER(NfqHandle)
#open_nfnl.argtypes = ctypes.POINTER(NfnlHandle),

# This function closes the nfqueue handler and free associated resources.
close_queue = netfilter.nfq_close
close_queue.restype = ctypes.c_int
close_queue.argtypes = ctypes.POINTER(NfqHandle),

# Bind a nfqueue handler to a given protocol family.
bind_pf = netfilter.nfq_bind_pf
bind_pf.restype = ctypes.c_int
bind_pf.argtypes = ctypes.POINTER(NfqHandle), ctypes.c_uint16

# Unbind nfqueue handler from a protocol family.
unbind_pf = netfilter.nfq_unbind_pf
unbind_pf.restype = ctypes.c_int
unbind_pf.argtypes = ctypes.POINTER(NfqHandle), ctypes.c_uint16

# Creates a new queue handle, and returns it.
create_queue = netfilter.nfq_create_queue
create_queue.restype = ctypes.POINTER(NfqQHandler)
create_queue.argtypes = ctypes.POINTER(NfqHandle), ctypes.c_uint16, ctypes.c_void_p, ctypes.c_void_p

# Removes the binding for the specified queue handle.
destroy_queue = netfilter.nfq_destroy_queue
destroy_queue.restype = ctypes.c_int
destroy_queue.argtypes = ctypes.POINTER(NfqQHandler),

# Triggers an associated callback for the given packet received from the queue.
handle_packet = netfilter.nfq_handle_packet
handle_packet.restype = ctypes.c_int
handle_packet.argtypes = ctypes.POINTER(NfqHandle), ctypes.c_char_p, ctypes.c_int

# nfqnl_config_mode
NFQNL_COPY_NONE, NFQNL_COPY_META, NFQNL_COPY_PACKET = 0, 1, 2

# Sets the amount of data to be copied to userspace for each packet queued
# to the given queue.
#
# NFQNL_COPY_NONE - do not copy any data
# NFQNL_COPY_META - copy only packet metadata
# NFQNL_COPY_PACKET - copy entire packet
set_mode = netfilter.nfq_set_mode
set_mode.restype = ctypes.c_int
set_mode.argtypes = ctypes.POINTER(NfqQHandler), ctypes.c_uint8, ctypes.c_uint

# Sets the size of the queue in kernel. This fixes the maximum number
# of packets the kernel will store before internally before dropping
# upcoming packets.
set_queue_maxlen = netfilter.nfq_set_queue_maxlen
set_queue_maxlen.restype = ctypes.c_int
set_queue_maxlen.argtypes = ctypes.POINTER(NfqQHandler), ctypes.c_uint32

# Responses from hook functions.
NF_DROP, NF_ACCEPT, NF_STOLEN = 0, 1, 2
NF_QUEUE, NF_REPEAT, NF_STOP = 3, 4, 5
NF_MAX_VERDICT = NF_STOP

# Notifies netfilter of the userspace verdict for the given packet. Every
# queued packet _must_ have a verdict specified by userspace, either by
# calling this function, or by calling the nfq_set_verdict_mark() function.
# NF_DROP - Drop packet
# NF_ACCEPT - Accept packet
# NF_STOLEN - Don't continue to process the packet and not deallocate it.
# NF_QUEUE - Enqueue the packet
# NF_REPEAT - Handle the same packet
# NF_STOP -
# NF_MAX_VERDICT -
set_verdict = netfilter.nfq_set_verdict
set_verdict.restype = ctypes.c_int
set_verdict.argtypes = ctypes.POINTER(NfqQHandler), ctypes.c_uint32, ctypes.c_uint32,\
	ctypes.c_uint32, ctypes.c_char_p

# Like set_verdict, but you can set the mark.
set_verdict_mark = netfilter.nfq_set_verdict_mark
set_verdict_mark.restype = ctypes.c_int
set_verdict_mark.argtypes = ctypes.POINTER(NfqQHandler), ctypes.c_uint32, ctypes.c_uint32,\
	ctypes.c_uint32, ctypes.c_uint32, ctypes.c_char_p

# Return the metaheader that wraps the packet.
get_msg_packet_hdr = netfilter.nfq_get_msg_packet_hdr
get_msg_packet_hdr.restype = ctypes.POINTER(NfqnlMsgPacketHdr)
get_msg_packet_hdr.argtypes = ctypes.POINTER(NfqData),

# Return the netfilter mark currently assigned to the given queued packet.
get_nfmark = netfilter.nfq_get_nfmark
get_nfmark.restype = ctypes.c_uint32
get_nfmark.argtypes = ctypes.POINTER(NfqData),

# Retrieves the received timestamp when the given queued packet.
get_timestamp = netfilter.nfq_get_timestamp
get_timestamp.restype = ctypes.c_int
get_timestamp.argtypes = ctypes.POINTER(NfqData), ctypes.POINTER(Timeval)

# Return the index of the device the queued packet was received via.	 If the
# returned index is 0, the packet was locally generated or the input
# interface is not known.
get_indev = netfilter.nfq_get_indev
get_indev.restype = ctypes.c_uint32
get_indev.argtypes = ctypes.POINTER(NfqData),

# Return the index of the physical device the queued packet was received via.
# If the returned index is 0, the packet was locally generated or the
# physical input interface is no longer known.
get_physindev = netfilter.nfq_get_physindev
get_physindev.restype = ctypes.c_uint32
get_physindev.argtypes = ctypes.POINTER(NfqData),

# Return the index of the device the queued packet will be sent out.
get_outdev = netfilter.nfq_get_outdev
get_outdev.restype = ctypes.c_uint32
get_outdev.argtypes = ctypes.POINTER(NfqData),

# Return The index of physical interface that the packet output will be routed out
get_physoutdev = netfilter.nfq_get_physoutdev
get_physoutdev.restype = ctypes.c_uint32
get_physoutdev.argtypes = ctypes.POINTER(NfqData),


##################################################################
# Not implemented yet.
##################################################################

#get_indev_name = netfilter.nfq_get_indev_name
#get_indev_name.restype = ctypes.c_int
#get_indev_name.argtypes = ctypes.POINTER(NlifHandle), ctypes.POINTER(NfqData), ctypes.c_void_p

#def test_get_indev_name(nfa):
#	ptr_name = ctypes.c_void_p(0)
#	nlif = NlifHandle()
#	get_indev_name(ctypes.byref(nlif), nfa, ptr_name)
#	print(ptr_name)

#get_physindev_name = netfilter.nfq_get_physindev_name
#get_physindev_name.restype = ctypes.c_int
#get_physindev_name.argtypes = ctypes.POINTER(NlifHandle), ctypes.POINTER(NfqData), ctypes.c_char_p
########

#get_outdev_name = netfilter.nfq_get_outdev_name
#get_outdev_name.restype = ctypes.c_int
#get_outdev_name.argtypes = ctypes.POINTER(NlifHandle), ctypes.POINTER(NfqData), ctypes.c_char_p
########

#get_physoutdev_name = netfilter.nfq_get_physoutdev_name
#get_physoutdev_name.restype = ctypes.c_int
#get_physoutdev_name.argtypes = ctypes.POINTER(NlifHandle), ctypes.POINTER(NfqData), ctypes.c_char_p
########

# Retrieves the hardware address associated with the given queued packet.
get_packet_hw = netfilter.nfq_get_packet_hw
get_packet_hw.restype = ctypes.POINTER(NfqnlMsgPacketHw)
get_packet_hw.argtypes = ctypes.POINTER(NfqData),

# Retrieve the payload for a queued packet.
get_payload = netfilter.nfq_get_payload
get_payload.restype = ctypes.c_int
get_payload.argtypes = ctypes.POINTER(NfqData), ctypes.POINTER(ctypes.c_void_p)


HANDLER = ctypes.CFUNCTYPE(
	#(struct NfqQHandler *qh, struct nfgenmsg *nfmsg, struct NfqData *nfa, void *data)
	None, *(ctypes.POINTER(NfqQHandler), ctypes.c_void_p, ctypes.POINTER(NfqData), ctypes.c_void_p)
)


#def open_queue():
#	handler = ll_open_queue()
#	assert handler is not None, "can't open the queue"
#	return handler


def get_full_payload(nfa, ptr_packet):
	len_recv = get_payload(nfa, ctypes.byref(ptr_packet))
	data = ctypes.string_at(ptr_packet, len_recv)
	return len_recv, data


#def get_full_msg_packet_hdr(nfa):
#	pkg_hdr = get_msg_packet_hdr(nfa)
#	return {"packet_id": ntohl(pkg_hdr.contents.packet_id),
#		"hw_protocol": ntohl(pkg_hdr.contents.hw_protocol),
#		"hook": pkg_hdr.contents.hook}

#def get_packet_id(nfa):
#	pkg_hdr = get_msg_packet_hdr(nfa)
#	return ntohl(pkg_hdr.contents.packet_id)

#def get_pytimestamp(nfa):
#	mtime = Timeval()
#	get_timestamp(nfa, ctypes.byref(mtime))
#	return mtime.tv_sec, mtime.tv_usec


class Interceptor(object):
	"""
	Packet interceptor. Allows MITM and filtering.
	Example config for iptables:
	iptables -I INPUT 1 -p icmp -j NFQUEUE --queue-balance 0:2
	"""
	QueueConfig = namedtuple("QueueConfig",
		["queue", "queue_id", "nfq_handle", "nfq_socket", "verdictthread", "handler",
		"packet_ptr"])

	def __init__(self):
		self._netfilterqueue_configs = []
		self._is_running = False

	@staticmethod
	def verdict_trigger_cycler(recv, nfq_handle, obj):
		try:
			while obj._is_running:
				try:
					bts = recv(65535)
				except socket_timeout:
					continue

				handle_packet(nfq_handle, bts, 65535)
		except OSError as ex:
			# eg "Bad file descriptor": started and nothing read yet
			#logger.debug(ex)
			pass
		except Exception as ex:
			logger.debug("Exception while reading: %r", ex)
		#finally:
		#	logger.debug("verdict_trigger_cycler finished, stopping Interceptor")
		#	obj.stop()

	def _setup_queue(self, queue_id, ctx, verdict_callback):
		logger.debug("setup queue with id %d", queue_id)
		packet_ptr = ctypes.c_void_p(0)

		def verdict_callback_ind(queue_handle, nfmsg, nfa, _data):
			# logger.debug("verdict cb for queue %d", queue_id)
			pkg_hdr = get_msg_packet_hdr(nfa)
			packet_id = ntohl(pkg_hdr.contents.packet_id)
			linklayer_protoid = htons(pkg_hdr.contents.hw_protocol)
			len_recv, data = get_full_payload(nfa, packet_ptr)
			# data_ret, verdict = data, NF_ACCEPT
			data_ret, verdict = verdict_callback(data, linklayer_protoid, ctx)
			set_verdict(queue_handle, packet_id, verdict, len(data_ret), ctypes.c_char_p(data_ret))

		nfq_handle = ll_open_queue()  # 2

		unbind_pf(nfq_handle, socket.AF_INET)
		bind_pf(nfq_handle, socket.AF_INET)

		c_handler = HANDLER(verdict_callback_ind)
		queue = create_queue(nfq_handle, queue_id, c_handler, None)  # 1

		set_mode(queue, NFQNL_COPY_PACKET, 0xFFFF)

		nf = nfnlh(nfq_handle)
		fd = nfq_fd(nf)
		# fd, family, sockettype
		nfq_socket = socket.fromfd(fd, 0, 0)  # 3
		# TODO: better solution to check for running state? close socket and raise exception does not work in stop()
		nfq_socket.settimeout(1)

		thread = threading.Thread(
			target=Interceptor.verdict_trigger_cycler,
			args=[nfq_socket.recv, nfq_handle, self]
		)

		thread.start()

		qconfig = Interceptor.QueueConfig(
			queue=queue, queue_id=queue_id, nfq_handle=nfq_handle, nfq_socket=nfq_socket,
			verdictthread=thread, packet_ptr=packet_ptr, handler=c_handler
		)
		self._netfilterqueue_configs.append(qconfig)

	def start(self, verdict_callback, queue_ids, ctx=None):
		"""
		verdict_callback -- callback with this signature:
			callback(data, ctx): data, verdict
		queue_id -- id of the que placed using iptables
		ctx -- context object given to verdict callback
		"""
		if self._is_running:
			return

		if queue_ids is None:
			queue_ids = []

		self._is_running = True

		for queue_id in queue_ids:
			# setup queue and start produces threads
			self._setup_queue(queue_id, ctx, verdict_callback)

	def stop(self):
		if not self._is_running:
			return

		logger.debug("stopping Interceptor")
		self._is_running = False

		for qconfig in self._netfilterqueue_configs:
			destroy_queue(qconfig.queue)
			close_queue(qconfig.nfq_handle)
			qconfig.nfq_socket.close()
			logger.debug("joining verdict thread for queue %d", qconfig.queue_id)
			qconfig.verdictthread.join()

		self._netfilterqueue_configs.clear()
