import logging
import os
import pickle
import socket
import struct
import select
import time
import json
from threading import Timer

import messages
from ring import Ring
from request import Request
from storage import Storage
from collections import defaultdict


class Node(object):

    def __init__(self, is_leader, leader_hostname, my_hostname, tcp_port=13337, sloppy_Qsize=5, sloppy_R=3, sloppy_W=3):

        self.ongoing_requests = []
        self.is_leader = is_leader
        self.leader_hostname = leader_hostname
        self.hostname = my_hostname
        self.tcp_port = tcp_port
        self.my_address = (self.hostname, self.tcp_port)

        self.membership_ring = Ring(replica_count=sloppy_Qsize - 1)  # Other nodes in the membership
        if self.is_leader:
            self.membership_ring.add_node(leader_hostname)

        # todo: look into this, do we need both?
        self.bootstrapping = True
        self.is_member = False

        # Flag to keep track of a add-node is underway
        self._membership_in_progress = False

        self.sloppy_Qsize = sloppy_Qsize  # total members to replicate on

        # number of peers required for a read or write to succeed.
        self.sloppy_R = sloppy_R
        self.sloppy_W = sloppy_W

        # Book keeping for membership messages
        self._req_responses = defaultdict(set)
        self._sent_req_messages = {}
        self._received_req_messages = {}
        self._req_sender = {}  # Keeps track to sender for add and delete requests
        self.current_view = 0  # increment this on every leader election
        self.membership_request_id = 0  # increment this on every request sent to peers

        # Maintains handoff messages to be sent
        # IP : set(handoff messages)
        self._handoff_messages = defaultdict(set)
        self.handoff_timer = None
        self.create_handoff_timer = lambda: Timer(5, self.try_sending_handoffs)

        self.log_prefix = os.getcwd()
        self.ring_log_file = os.path.join(self.log_prefix, self.hostname + '.ring')
        self.db_path = os.path.join(self.log_prefix, self.hostname + '.db')
        self.handoff_log = os.path.join(self.log_prefix, self.hostname + '.pickle')

        try:
            with open(self.ring_log_file, 'r') as f:
                hosts = f.readlines()
                for h in hosts:
                    self.membership_ring.add_node(h.strip())
            print("Restored membership information from %s" % self.ring_log_file)
        except FileNotFoundError:
            pass

        try:
            with open(self.handoff_log, 'rb') as f:
                self._handoff_messages = pickle.loads(f.read())

            if len(self._handoff_messages) > 0:
                self.handoff_timer = self.create_handoff_timer()
                self.handoff_timer.start()

            print("Restored hand off messages from %s" % self.handoff_log)
        except FileNotFoundError:
            pass

        self.request_timelimit = 2.0
        self.req_message_timers = {}

        self.db = Storage(self.db_path)  # set up sqlite table

        # create tcp socket for communication with peers and clients
        self.tcp_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.tcp_socket.setblocking(False)  # Non-blocking socket
        self.tcp_socket.bind((self.hostname, self.tcp_port))
        self.tcp_socket.listen(10)

        # has hostnames mapped to open sockets
        self.connections = {}
        self.client_list = set()

    def accept_connections(self):
        incoming_connections = {self.tcp_socket}
        print("Accepting connections...")

        while True:
            readable, _, _ = select.select(incoming_connections, [], [], 0)
            for s in readable:
                if s is self.tcp_socket:
                    connection, client_address = s.accept()
                    connection.setblocking(False)

                    incoming_connections.add(connection)
                    self.connections[client_address[0]] = connection

                else:
                    try:
                        header = s.recv(5)
                    except:
                        print("Connection reset.")
                        continue

                    if not header:  # remove for connection pool and close socket
                        incoming_connections.remove(s)
                        # del self.connections[s.getpeername()[0]]
                        s.close()
                    else:
                        message_len = struct.unpack('!i', header[1:5])[0]

                        data = b''
                        while len(data) < message_len:
                            try:
                                data += s.recv(message_len - len(data))
                            except socket.error as err:
                                pass

                        self._process_message(header + data, s.getpeername()[0])  # addr is a tuple of hostname and port

    def _process_message(self, data, sender):
        message_type, data_tuple = messages._unpack_message(data)

        message_type_mapping = {
            b'\x00': self._process_command,
            b'\x01': self._process_req_message,
            b'\x10': self._membership_change_message,
            b'\xff': self._process_ok_message,
            b'\x07': self.perform_operation,
            b'\x08': self.perform_operation,
            b'\x70': self.update_request,
            b'\x80': self.update_request,
            b'\x0B': self.update_request,
            b'\x0A': self.handle_forwarded_req,
            b'\x0C': self.handle_handoff
        }

        message_type_mapping[message_type](data_tuple, sender)
        return

    def _process_command(self, user_input, sendBackTo):
        """Process commands"""

        self.client_list.add(sendBackTo)

        # Maps command to the corresponding function.
        # Command arguments are passed as the first argument to the function.
        command_registry = {  # Possible commands:
            "add-node": self.add_node,  # 1. add node to membership
            "remove-node": self.remove_node,  # 2. remove node from membership
            "put": self.put_data,  # 3. put data
            "get": self.get_data,  # 4. get data
        }

        if not user_input:
            self._send_req_response_to_client(sendBackTo, "User input empty")

        # First word is command. Rest are then arguments.
        command, *data = user_input.split(" ")
        if command not in command_registry:
            self._send_req_response_to_client(sendBackTo, "Invalid command")

        # Call the function associated with the command in command_registry
        return command_registry[command](data, sendBackTo)

    def handle_handoff(self, data, sendBackTo):
        # data should have (message, list of hosts to hand data off to)

        for h in data[1]:
            print("Storing hand off message for %s" % h)
            self._handoff_messages[h].add(data[0])

        # Save handoff messages to disk
        self.sync_handoffs_to_disk()

        # check and start timer
        if not self.handoff_timer:
            self.handoff_timer = self.create_handoff_timer()
            self.handoff_timer.start()

    def try_sending_handoffs(self):
        print("Attempting to send hand off messages...")

        updated_handoff_messages = defaultdict(set)

        for (host, msgs) in self._handoff_messages.items():
            for msg in msgs:
                fail = self.broadcast_message([host], msg)
                if fail:
                    updated_handoff_messages[host].add(msg)

        self._handoff_messages = updated_handoff_messages
        self.sync_handoffs_to_disk()

        if len(self._handoff_messages) > 0:
            print("Undelivered hand offs. Restarting timer")
            self.handoff_timer = self.create_handoff_timer()
            self.handoff_timer.start()
            return

        print("Sent all hand off messages.")
        self.handoff_timer = None

    def sync_handoffs_to_disk(self):
        # Save hand off messages to disk
        with open(self.handoff_log, 'wb') as f:
            f.write(pickle.dumps(self._handoff_messages))

    def add_node(self, data, sender):
        """Add node to membership. data[0] must be the hostname. Initiates 2PC."""

        if not self.is_leader:
            self._send_req_response_to_client(sender, "Error: This is not the leader")
            return

        if not data:
            self._send_req_response_to_client(sender, "Error: hostname required")
            return

        if self._membership_in_progress:
            self._send_req_response_to_client(sender, "Membership operation in progress. Try again")
            return

        if data[0] in self.membership_ring:
            self._send_req_response_to_client(sender, "Already in the membership")
            return

        self._membership_in_progress = True

        print("Starting add-node operation for %s" % data[0])
        new_peer_message = messages.reqMessage(self.current_view, self.membership_request_id, 1, data[0])

        # associate hostname to (view_id, req_id)
        self._sent_req_messages[(self.current_view, self.membership_request_id)] = (data[0], 1)
        self._req_sender[(self.current_view, self.membership_request_id)] = sender

        # broadcast to all but leader.
        nodes_to_broadcast = self.membership_ring.get_all_hosts()
        nodes_to_broadcast.remove(self.hostname)
        nodes_to_broadcast.add(data[0])  # Add new host to broadcast list

        self.broadcast_message(nodes_to_broadcast, new_peer_message)

        t = Timer(self.request_timelimit, self._req_timeout, args=[(self.current_view, self.membership_request_id)])
        self.req_message_timers[(self.current_view, self.membership_request_id)] = t
        t.start()

        self.membership_request_id += 1

    # Send a remove node message to everyone and if you are that node, shutdown
    def remove_node(self, data, sender):
        if not self.is_leader:
            self._send_req_response_to_client(sender, "Error: This is not the leader")
            return

        if not data:
            self._send_req_response_to_client(sender, "Error: hostname required")
            return

        if self._membership_in_progress:
            self._send_req_response_to_client(sender, "Membership operation in progress. Try again")
            return

        if data[0] not in self.membership_ring:
            self._send_req_response_to_client(sender, "Cannot remove. Node not in membership")
            return

        self._membership_in_progress = True
        print("Starting remove-node operation for %s" % data[0])
        new_peer_message = messages.reqMessage(self.current_view, self.membership_request_id, 2, data[0])

        # associate hostname to (view_id, req_id)
        self._sent_req_messages[(self.current_view, self.membership_request_id)] = (data[0], 2)
        self._req_sender[(self.current_view, self.membership_request_id)] = sender

        # broadcast to all but leader.
        nodes_to_broadcast = self.membership_ring.get_all_hosts()
        nodes_to_broadcast.remove(self.hostname)
        nodes_to_broadcast.add(data[0])  # Add new host to broadcast list

        self.broadcast_message(nodes_to_broadcast, new_peer_message)

        t = Timer(self.request_timelimit, self._req_timeout, args=[(self.current_view, self.membership_request_id)])
        self.req_message_timers[(self.current_view, self.membership_request_id)] = t
        t.start()

        self.membership_request_id += 1

        # self.membership_ring.remove_node(data[0])

    def put_data(self, data, sendBackTo):
        if len(data) != 3:
            return "Error: Invalid operands\nInput: (<key>,<prev version>,<value>)"

        data = [data[0], json.loads(data[1]), data[2]]
        key = data[0]
        prev = data[1]
        value = data[2]
        target_node = self.membership_ring.get_node_for_key(data[0])
        if not self.is_leader:
            # forward request to leader for client
            return self._send_data_to_peer(self.leader_hostname, data, sendBackTo)

        else:  # I am the leader
            if target_node == self.hostname:
                # I'm processing a request for a client directly
                self.start_request('put', data, sendBackTo=sendBackTo)
                return

            else:  # I am forwarding a request from the client to the correct node
                return self._send_data_to_peer(target_node, data, sendBackTo)

    def get_data(self, data, sendBackTo):
        """Retrieve V for given K from the database. data[0] must be the key"""
        if not data:
            return "Error: key required"

        target_node = self.membership_ring.get_node_for_key(data[0])
        # if I can do it myself
        if target_node == self.hostname:
            # I am processing a request for a client directly
            self.start_request('get', data[0], sendBackTo=sendBackTo)

        else:  # forward the client request to the peer incharge of req
            self._request_data_from_peer(target_node, data[0], sendBackTo)

    def _process_req_message(self, data, sender):
        # data = (view_id, req_id, operation, address)
        (view_id, req_id, operation, address) = data

        print("Processed request message to add %s" % address)
        # save the message type
        self._received_req_messages[(view_id, req_id)] = (address, operation)
        ok_message = messages.okMessage(view_id, req_id)
        self.connections.get(sender, self._create_socket(sender)).sendall(ok_message)

    def _process_ok_message(self, data, sender):
        self._req_responses[data].add(sender)
        (new_peer_hostname, operation) = self._sent_req_messages[data]
        required_responses = len(self.membership_ring) if operation == 1 else (len(self.membership_ring) - 1)

        # number of replies equal number of *followers* already in the ring, add peer to membership
        if len(self._req_responses[data]) == required_responses:

            # Cancel timer
            t = self.req_message_timers.get(data, None)
            if not t:
                return
            t.cancel()

            # Send newViewMessage
            # self.current_view += 1

            if operation == 1:   # adding new node
                self.membership_ring.add_node(new_peer_hostname)
                hosts_to_send = self.membership_ring.get_all_hosts()
                nodes_to_broadcast = self.membership_ring.get_all_hosts()
            else:
                hosts_to_send = (new_peer_hostname, )
                nodes_to_broadcast = self.membership_ring.get_all_hosts()
                nodes_to_broadcast.remove(new_peer_hostname)
                self.membership_ring.remove_node(new_peer_hostname)

            membership_change_msg = messages.membershipChange(self.current_view, operation, hosts_to_send)

            nodes_to_broadcast.remove(self.hostname)
            self.broadcast_message(nodes_to_broadcast, membership_change_msg)

            print("Successfully %s %s." % ("added" if operation == 1 else "removed", new_peer_hostname))

            client = self._req_sender.get(data, None)
            self._send_req_response_to_client(client, "Successfully %s %s." %
                                              ("added" if operation == 1 else "removed", new_peer_hostname))

            self._membership_in_progress = False  # reset state to accept new connections

    def _membership_change_message(self, data, sender):
        (view_id, operation, peers) = data
        # self.current_view = view_id

        if operation == 1:  # add nodes
            for p in peers:
                if p not in self.membership_ring:
                    self.membership_ring.add_node(p)

        if operation == 2:
            for p in peers:
                if p in self.membership_ring:
                    self.membership_ring.remove_node(p)

        with open(self.ring_log_file, 'w') as f:
            for node in self.membership_ring.get_all_hosts():
                f.write(node + '\n')

        print("Successfully modified membership ring. Total members: %d" % len(self.membership_ring.get_all_hosts()))
        print("Current members: %s" % ", ".join(self.membership_ring.get_all_hosts()))
        print("Keys to manage: ", self.membership_ring.get_key_range(self.hostname))

    def _req_timeout(self, req_id):
        print("Error adding node to network. One or more nodes is offline.")

        # failed_hosts = set(self.membership_ring.get_all_hosts()) - self._req_responses[req_id]
        # failed_hosts.remove()
        sender = self._req_sender.get(req_id, None)
        self._send_req_response_to_client(sender, "Failed to add node to the network")
        self._membership_in_progress = False

    def _send_req_response_to_client(self, client, message):
        msg = messages.responseForForward(message)
        self.broadcast_message([client], msg)

    # request format:
    # object which contains
    # type
    # sendBackTo
    # forwardedTo =None if type is not for_*
    # hash
    # value =None if type is get or forget
    # context =None if type is get or forget
    # responses = { sender:msg, sender2:msg2... }

    # args format is determined by type:
    #   type='get', args='hash'
    #   type='put', args=('hash','value',{context})
    #   type='for_get', args=(target_node,'hash')
    #   type='for_put', args=(target_node, 'hash','value',{context})
    # Type can be 'put', 'get', 'for_put', 'for_get'
    # 'for_*' if for requests that must be handled by a different peer
    # then when the response is returned, complete_request will send the
    # output to the correct client or peer (or stdin)
    def start_request(self, rtype, args, sendBackTo, prev_req=None):
        print("%s request from %s: %s" % (rtype, sendBackTo, args))
        req = Request(rtype, args, sendBackTo, previous_request=prev_req)  # create request obj
        self.ongoing_requests.append(req)  # set as ongoing

        target_node = self.membership_ring.get_node_for_key(req.hash)
        replica_nodes = self.membership_ring.get_replicas_for_key(req.hash)

        T = Timer(self.request_timelimit + (1 if rtype[:3] == 'for' else 0),
                  self.complete_request, args=[req], kwargs={"timer_expired": True}
                  )
        T.start()
        self.req_message_timers[req.time_created] = T

        # Find out if you can respond to this request
        if rtype == 'get':
            # add my information to the request
            result = self.db.getFile(args)
            my_resp = messages.getFileResponse(args, result, req.time_created)
            self.update_request(messages._unpack_message(my_resp)[1], socket.gethostbyname(self.hostname), req)
            # send the getFile message to everyone in the replication range
            msg = messages.getFile(req.hash, req.time_created)
            # this function will need to handle hinted handoff

            print("Sending getFile message to %s" % ", ".join(replica_nodes))
            fails = self.broadcast_message(replica_nodes, msg)
            if fails:
                print("Failed to send get msg to %s" % ', '.join(fails))

        elif rtype == 'put':
            self.db.storeFile(args[0], socket.gethostbyname(self.hostname), args[1], args[2])
            my_resp = messages.storeFileResponse(args[0], args[1], args[2], req.time_created)
            # add my information to the request
            self.update_request(messages._unpack_message(my_resp)[1], socket.gethostbyname(self.hostname), req)
            # send the storeFile message to everyone in the replication range
            msg = messages.storeFile(req.hash, req.value, req.context, req.time_created)
            # this function will need to handle hinted handoff
            print("Sending storeFile message to %s" % ", ".join(replica_nodes))
            fails = self.broadcast_message(replica_nodes, msg)
            if fails:
                print("Failed to send put msg to %s" % ', '.join(fails))

        else:
            msg = messages.forwardedReq(req)
            # forward message to target node
            # self.connections[req.forwardedTo].sendall(msg)
            if self.broadcast_message([req.forwardedTo], msg):
                self.leader_to_coord(req)
            else:
                print("Forwarded Request to %s" % req.forwardedTo)

    def leader_to_coord(self, req):
        print("Leader is assuming role of coordinator")
        replica_nodes = self.membership_ring.get_replicas_for_key(req.hash)
        req.type = req.type[4:]

        if req.type == 'get':
            msg = messages.getFile(req.hash, req.time_created)
        else:
            msg = messages.storeFile(req.hash, req.value, req.context, req.time_created)

        self.broadcast_message(replica_nodes, msg)

    def find_req_for_msg(self, req_ts):
        return list(filter(
            lambda r: r.time_created == req_ts, self.ongoing_requests
        ))

    # after a \x70, \x80 or \x0B is encountered from a peer, this method is called
    def update_request(self, msg, sender, request=None):
        print("Updating Request with message ", msg, " from ", sender)
        if isinstance(msg, tuple):
            if not request:
                request = self.find_req_for_msg(msg[-1])
            min_num_resp = self.sloppy_R if len(msg) == 3 else self.sloppy_W
        else:
            request = self.find_req_for_msg(msg.previous_request.time_created)
            min_num_resp = 1

        if not request:
            print("No request found, ", sender, " might have been too slow")
            return
        elif isinstance(request, list):
            request = request[0]

        request.responses[sender] = msg
        if len(request.responses) >= min_num_resp:
            self.complete_request(request)

    def coalesce_responses(self, request):
        resp_list = list(request.responses.values())
        # check if you got a sufficient number of responses
        if len(resp_list) < self.sloppy_R:
            return None
        results = []
        for resp in resp_list:
            # print(resp)
            results.extend([
                tup for tup in resp[1] if tup not in results
            ])
        return self.db.sortData(results)

    def complete_request(self, request, timer_expired=False):

        failed = False

        if request.type == 'get':
            # if sendbackto is a peer
            if request.sendBackTo not in self.client_list:
                # this is a response to a for_*
                # send the whole request object back to the peer
                msg = messages.responseForForward(request)
            else:
                # compile results from responses and send them to client
                # send message to client
                msg = messages.getResponse(request.hash, (
                    self.coalesce_responses(request) if not failed else "Error"
                ))

        elif request.type == 'put':
            if len(request.responses) >= self.sloppy_W:
                print("Successful put completed for ", request.sendBackTo)

            if request.sendBackTo not in self.client_list:
                # this is a response to a for_*
                # send the whole request object back to the peer
                msg = messages.responseForForward(request)
            else:
                # send success message to client
                # check if you were successful
                msg = messages.putResponse(request.hash, (
                    request.value if len(request.responses) >= self.sloppy_W and not failed else "Error"
                ), request.context)

            # if len(request.responses) >= self.sloppy_W and timer_expired:
            if timer_expired and len(request.responses) < self.sloppy_Qsize:
                target_node = self.membership_ring.get_node_for_key(request.hash)
                replica_nodes = self.membership_ring.get_replicas_for_key(request.hash)

                all_nodes = set([target_node] + replica_nodes)
                missing_reps = set([self.membership_ring.hostname_to_ip[r] for r in all_nodes]) - set(request.responses.keys())

                handoff_store_msg = messages.storeFile(request.hash, request.value, request.context, request.time_created)

                handoff_msg = messages.handoff(
                    handoff_store_msg,
                    missing_reps
                )

                hons = [
                    self.membership_ring.get_handoff_node(r)
                    for r in missing_reps
                ]

                print("Handing off messages for %s to %s" % (", ".join(missing_reps), ", ".join(hons)))
                if self.hostname in hons:
                    self.handle_handoff((handoff_store_msg, missing_reps), self.hostname)
                    hons.remove(self.hostname)
                self.broadcast_message(hons, handoff_msg)

        else:  # request.type == for_*
            # unpack the forwarded request object
            data = list(request.responses.values())

            if not data:
                # del self.req_message_timers[request.time_created]
                # request.time_created=time.time()
                self.leader_to_coord(request)
                T = Timer(self.request_timelimit,
                          self.complete_request, args=[request], kwargs={"timer_expired": True}
                          )
                T.start()
                self.req_message_timers[request.time_created] = T
                return
            else:
                data = data[0]
                # if sendbackto is a peer
                if request.sendBackTo not in self.client_list:
                    # unpickle the returned put request
                    data.previous_request = data.previous_request.previous_request
                    # send the response object you got back to the peer
                    # from request.responses (it is the put or get they need)
                    # if you need to, make req.prev_req = req.prev_req.prev_req
                    # so it looks like you did the request yourself
                    msg = messages.responseForForward(data)
                elif request.type == 'for_put':
                    msg = messages.putResponse(request.hash, (
                        request.value if data and len(data.responses) >= self.sloppy_W and not failed else "Error"
                    ), request.context)
                else:  # for_get
                    msg = messages.getResponse(request.hash, (
                        self.coalesce_responses(data) if not failed else "Error"
                    ))

        # send msg to request.sendBackTo
        # if request.sendBackTo not in self.client_list:
        if not request.responded:
            print("Sending response back to ", request.sendBackTo)
            self.broadcast_message([request.sendBackTo], msg)
            request.responded = True

        if timer_expired:
            # remove request from ongoing list
            self.ongoing_requests = list(filter(
                lambda r: r.time_created != request.time_created, self.ongoing_requests
            ))

    def perform_operation(self, data, sendBackTo):
        if len(data) == 2:  # this is a getFile msg
            print("%s is asking me to get %s" % (sendBackTo, data[0]))
            msg = messages.getFileResponse(data[0], self.db.getFile(data[0]), data[1])
        else:  # this is a storeFile
            print("%s is asking me to store %s" % (sendBackTo, data[0]))
            self.db.storeFile(data[0], sendBackTo, data[1], data[2])
            msg = messages.storeFileResponse(*data)

        self.broadcast_message([sendBackTo], msg)

    def handle_forwarded_req(self, prev_req, sendBackTo):
        target_node = self.membership_ring.get_node_for_key(prev_req.hash)
        print("Handling a forwarded request [ %s, %f ]" % (prev_req.type, prev_req.time_created))

        if time.time() - prev_req.time_created < self.request_timelimit:
            # someone forwarded you a put request
            # if you are the leader, check if you can takecare of it, else,
            # start a new put request with this request as the previous one
            if prev_req.type == 'put' or prev_req.type == 'for_put':
                if self.is_leader:
                    if target_node == self.hostname:
                        args = (prev_req.hash, prev_req.value, prev_req.context)
                        self.start_request('put', args, sendBackTo, prev_req=prev_req)
                    else:
                        args = (target_node, prev_req.hash,
                                prev_req.value, prev_req.context
                                )
                        self.start_request('for_put', args, sendBackTo, prev_req)

                else:  # the leader is forwarding you a put
                    args = (prev_req.hash, prev_req.value, prev_req.context)
                    self.start_request('put', args, sendBackTo, prev_req=prev_req)

            # someone forwarded you a get request, you need to take care of it
            # start new get request with this as the previous one
            else:  # type is get or for_get
                self.start_request('get', prev_req.hash, sendBackTo, prev_req)

    def _send_data_to_peer(self, target_node, data, sendBackTo):
        # create for_put request
        self.start_request('for_put', [target_node] + data, sendBackTo=sendBackTo)

    def _request_data_from_peer(self, target_node, data, sendBackTo):
        self.start_request('for_get', (target_node, data), sendBackTo=sendBackTo)

    def _create_socket(self, hostname):
        """Creates a socket to the host and adds it connections dict. Returns created socket object."""
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setblocking(False)
        s.settimeout(1)  # 10 seconds
        try:
            s.connect((hostname, self.tcp_port))
            self.connections[socket.gethostbyname(hostname)] = s
            return s
        except Exception as e:
            if hostname not in self.client_list:
                print("Error creating connection to %s: %s" % (hostname, e))
            return None

    # this is where we need to handle hinted handoff if a
    # peer is not responsive by asking another peer to hold the
    # message until the correct node recovers
    def broadcast_message(self, nodes, msg):
        fails = []
        for node in nodes:
            c = self.connections.get(node, self._create_socket(node))
            if not c:
                fails.append(node)
                continue
            c.sendall(msg)

        return fails
