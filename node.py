import socket
from collections import defaultdict
from storage import Storage
from ring import Ring
from math import floor
import select
import sys

import messages
from request import Request


class Node(object):

    def __init__(self, is_leader, leader_hostname, leader_port, my_hostname, tcp_port=13337, sloppy_Qfrac=0.34, sloppy_R=3, sloppy_W=3):

        self.is_leader = is_leader
        self.leader_hostname = leader_hostname
        self.leader_port=leader_port
        self.my_hostname = my_hostname
        self.tcp_port = tcp_port
        self.my_address=(self.my_hostname,self.tcp_port)

        self.membership_ring = Ring()  # Other nodes in the membership
        if self.is_leader:
            self.membership_ring.add_node(leader_hostname, leader_hostname)

        self.currently_adding_peer=False

        self.sloppy_Qfrac = sloppy_Qfrac  # fraction of total members to replicate on

        # sets self.sloppy_Qsize to the number of replications required
        self.update_SQsize = lambda: self.sloppy_Qsize=floor(len(self.membership_ring) * self.sloppy_Qfrac)
        self.update_SQsize()
        #number of peers required for a read or write to succeed.
        self.sloppy_R=sloppy_R
        self.sloppy_W=sloppy_W

        # saves all the pending membership messages
        self._membership_messages = defaultdict(set)
        self.current_view = 0  # increment this on every leader election
        self.request_id = 0  # increment this on every request sent to peers

        # Maps command to the corresponding function.
        # Command arguments are passed as the first argument to the function.
        self.command_registry = {               # Possible commands:
            "add-node": self.register_node,     # 1. add node to membership
            "remove-node": self.remove_node,    # 2. remove node from membership
            "put": self.put_data,               # 3. put data
            "get": self.get_data,               # 4. get data
            "delete": self.delete_data,         # 5. delete data
            "quit": lambda x: x                 # 6. Quit
        }

        #eventually need to change this so table is persistent across crashes
        self.db = Storage(':memory:')  # set up sqlite table in memory

        #create socket
        self.tcp_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        #bind socket to correct port
        self.tcp_socket.bind(self.my_hostname,self.tcp_port)
        self.tcp_socket.listen(5)

        #lists of connections
        self.unidentified_sockets=[]
        self.peer_sockets={}
        self.client_sockets={}

    def accept_connections(self):
        #moved to __init__
        # self.tcp_socket.bind((self.my_hostname, self.tcp_port))
        # self.tcp_socket.listen(5)

        while True:
            conn, addr = self.tcp_socket.accept()
            data = conn.recv(1024)  # can be less than 1024 for this application
            # todo: figure out a more appropriate buffer size
            if not data:
                continue

            self._process_message(data, addr[0])  # addr is a tuple of hostname and port

    def start(self):

        if is_leader:
            self.main_loop()
        else:
            self.accept_connections()

    def main_loop(self):

        self.ongoing_requests=[]

        print("entered main loop")
        print("++>",end='',flush=True)

        while True:

            #use select to check if server socket has inbound conn
            if select.select([self.tcp_socket],[],[],0)[0]:
                #if so, accept connection
                (sock,addr) = self.tcp_socket.accept()
                #put on unidentified conns list
                self.unidentified_sockets.append(sock)

            #use select to check peer conns for message
            if self.peer_sockets:
                readable=select.select(
                    list(self.peer_sockets.values()),[],[],0
                )[0]

                if readable:
                    pass
                #if new message from conn
                    #read it and process command

                    #if its a forwarded request
                        #Extract the contained message

                        #start_request(type,args,sendbackto=sock.getpeername()[0])
                    #elif its a response to a forwarded request 
                        #update request
                    #elif its a storeFile req
                        #take action and acknowledge
                    #elif its a StoreFileResponse
                        #update_request
                    #elif its a getFile req
                        #get result and respond
                    #elif its a getFileResponse
                        #updata_request
                    #elif add node
                        #call self.register_node
                    #elif remove node
                        #call self.remove_node
                
            #if a peer comes back online
                #check if there are any hinted handoffs that you need to get

            #use select to check client conns for message
                #if new message, process command



    def _process_message(self, data, sender):
        data_tuple = messages._unpack_message(data)
        if data_tuple[0] == 0:  # Message from client, second element should be user_input string
            result = self._process_command(data_tuple[1],sendBackTo=sender)
            print(result)

    def _process_command(self, user_input, sendBackTo):
        """Process commands"""
        if not user_input:
            return ""

        # no leader anymore, after a node has been boostrapped, being the leader means nothing
        # if not self.is_leader:
        #     return self.forward_request_to_leader(user_input)

        # First word is command. Rest are then arguments.
        command, *data = user_input.split(" ")
        if command not in self.command_registry:
            return "Invalid command"

        # Call the function associated with the command in command_registry
        if command == 'put' or command == 'get':
            return self.command_registry[command](data,sendBackTo)
        else:
            return self.command_registry[command](data)

    def register_node(self, data):
        """Add node to membership. data[0] must be the hostname"""
        if not data:
            return "Error: hostname required"

        if len(self.membership_ring) == 1:  # Only leader is in the ring. Just add.
            self.membership_ring.add_node(data[0], data[0])  # node id is same as hostname for now
            return "added " + data[0] + " to ring"

        # I dont think we need totem, we just need local failure detection
        # if a client sends remove node command, then we will manually
        # transfer necessary files away from the peer, before sending
        # a message to every peer to remove that node, when the node in
        # question gets that final remove message, it should kill itself.

        # likewise, for adding a peer, transfer all necessary files to new peer
        # if peer revceives all the files, send add message to all peers
        # expect replies from at least N/2
        # send commit message to all peers

        self.membership_ring.add_node(data[0], data[0])  # node id is same as hostname for now

        self.update_SQsize()
        # todo: if number of replicas goes up, need to find new peer and send them your files

        return "added " + data[0] + " to ring"

#Send a remove node message to everyone and if you are that node, shutdown
    def remove_node(self, data):
        if not data:
            return "Error: hostname required"

        self.membership_ring.remove_node(data[0])

        self.update_SQsize()
        # todo: update sloppy quorum size, if size changes
        # tell your lowest index replica to delete your files

        return "removed " + data[0] + " from ring"

    #request format:
    #object which contains 
        #type
        #sendBackTo
        #forwardedTo =None if type is not for_*
        #hash
        #value =None if type is get or forget
        #context =None if type is get or forget
        #responses = { sender:msg, sender2:msg2... }

    #args format is determined by type:
    #   type='get', args='hash'
    #   type='put', args=('hash','value',{context})
    #   type='for_get', args=(target_node,'hash')
    #   type='for_put', args=(target_node, 'hash','value',{context})
    #Type can be 'put', 'get', 'for_put', 'for_get'
    #'for_*' if for requests that must be handled by a different peer
    #then when the response is returned, complete_request will send the 
    #output to the correct client or peer (or stdin)
    def start_request(self, rtype, args, sendBackTo,prev_req=None):
        req = Request(rtype,args,sendBackTo)    #create request obj
        self.ongoing_requests.append(req)       #set as ongoing

        # data_nodes = self.membership_ring.get_node_for_key(self.sloppy_Qsize)
        target_node= self.membership_ring.get_node_for_key(req.hash)

        #Find out if you can respond to this request
        if rtype == 'get':
            #add my information to the request
            result = self.db.getFile(args)
            my_resp = getFileResponse(args, result)
            self.update_request(my_resp,self.my_hostname,req)
            #send the getFile message to everyone in the replication range

        elif rtype == 'put':
            self.db.storeFile(args[0],self.my_hostname,args[2],args[1])
            my_resp = storeFileResponse(args[0],args[1],args[2])
            #add my information to the request
            self.update_request(my_resp,self.my_hostname,req)
            #send the storeFile message to everyone in the replication range

        else:
            msg = forwardedReq(req)
            #forward message to target node

    #after a \x70, \x80 or \x0B is encountered from a peer, this method is called
    def update_request(self,msg,sender,request=None):
        # if not request:
            #find correct request corresponding to message
            #if 70 or 80
            #by checking the response id (aka timestamp of the req creation)
            #or by checking matching the response id of the msg's req's prev_req.time

        #request.responses[sender]=msg
        
        #if it is a \x0B
            # self.complete_request(request)
        #elif its a \x70 or \x80
            #number of responses required is min of (sloppy_R/W and the number of replicas)

            #if you have the min number of responses
                #complete_message(req)

    def complete_request(self,request):
        pass
        #if get
            #if sendbackto is a peer
                #this is a response to a for_*
                #send the whole request object back to the peer
            #else
                #compile results from responses and send them to client
        #elif put
            #if sendback is a peer
                #send the whole request object back to the peer
        #else
            #if sendbackto is a peer
                #send the response object you got back to the peer
                    #from request.responses (it is the put or get they need)
            #else
                #peer is sending you back the completed put or get
                #if put send back success
                #else, compile results and send back

        #remove request from ongoing list

    def put_data(self, data, sendBackTo):
        if len(data) != 3:
            return "Error: Invalid opperands\nInput: (<key>,<prev version>,<value>)"

        key = data[0]
        prev = data[1]
        value = data[2]
        target_node = self.membership_ring.get_node_for_key(data[0])

        if not self.is_leader:
            #forward request to leader for client
            return self._send_data_to_peer(self.leader_hostname,data,sendBackTo)
        else: #I am the leader
            if target_node == self.my_hostname:
                # I'm processing a request for a client directly
                self.start_request('put',data,sendBackTo=sendBackTo)
                return "started put request for %s:%s locally [%s]" % (key, value, self.my_hostname)
            else:# I am forwarding a request from the client to the correct node
                return self._send_data_to_peer(target_node,data,sendBackTo)

    def get_data(self, data, sendBackTo='stdin'):
        """Retrieve V for given K from the database. data[0] must be the key"""
        if not data:
            return "Error: key required"

        target_node = self.membership_ring.get_node_for_key(data[0])

        #if I can do it myself
        if target_node == self.my_hostname:
            #I am processing a request for a client directly
            self.start_request('get',data,sendBackTo=sendBackTo)
            return "started get request for %s:%s locally [%s]" % (
                data[0], self.db.getFile(data[0]), self.my_hostname
            )
        else:#forward the client request to the peer incharge of req
            return self._get_data_from_peer(target_node, data[0])

    def handle_forwarded_req(self,prev_req,sendBackTo):
        target_node = self.membership_ring.get_node_for_key(prev_req.hash)
        #someone forwarded you a put request
        #if you are the leader, check if you can takecare of it, else, 
        #start a new put request with this request as the previous one
        if prev_req.type == 'put' or prev_req.type == 'for_put':
            if self.is_leader:
                if target_node == self.my_hostname:
                    args=(prev_req.hash, prev_req.value, prev_req.context)
                    self.start_request('put',args,sendBackTo,previous_request=prev_req)
                    return "handling forwarded put request locally"
                else:
                    args=(target_node,prev_req.hash, 
                        prev_req.value, prev_req.context
                    )
                    self.start_request('for_put',args,sendBackTo,prev_req)
                    return "Forwarded Forwarded put request to correct node"
            else: #the leader is forwarding you a put
                args=(prev_req.hash, prev_req.value, prev_req.context)
                self.start_request('put',args,sendBackTo,previous_request=prev_req)
                return "handling forwarded put request locally"
        #someone forwarded you a get request, you need to take care of it
        #start new get request with this as the previous one
        else: #type is get or for_get
            start_request('get',prev_req.hash,sendBackTo,prev_req)
            return "handling forwarded get request locally"

    def _send_data_to_peer(self, target_node, data, sendBackTo='stdin'):

        #create for_put request
        start_request('for_put',(target_node,)+data,sendBackTo=sendBackTo)

        return "forwarded put request for %s:%s to node %s" % (key, value, target_node)

    def _get_data_from_peer(self, target_node, data, sendBackTo='stdin'):

        start_request('for_get',(target_node,data),sendBackTo=sendBackTo)

        return "forwarded get request for %s to node %s" % (target_node, key)

    # def delete_data(self, data):
    #     """Retrieve V for given K from the database. data[0] must be the key"""
    #     if not data:
    #         return "Error: key required"

    #     target_node = self.membership_ring.get_node_for_key(data[0])

    #     if target_node == self.my_hostname:
    #         self.db.remFile(data[0])
    #         return "deleted %s locally [%s]" % (data[0], self.my_hostname)
    #     else:
    #         return self._delete_data_from_peer(target_node, data[0])

    # def _delete_data_from_peer(self, target_node, key):
    #     return "deleted %s from node %s" % (target_node, key)

    # # todo: If a command is given to a follower, forward it to the leader. Come to it if time permits.
    # def forward_request_to_leader(self, user_input):
    #     """When a command is passed to the leader"""
    #     return "Forwarded request to " + self.leader_hostname
