import time
from random import random

import pymongo
from karait.model.message import Message
from bson.code import Code

class Queue(object):
    
    ALREADY_EXISTS_EXCEPTION_STRING = 'collection already exists'
    
    def __init__(
        self,
        host='localhost',
        port=27017,
        database='karait',
        queue='messages',
        average_message_size=8192,
        queue_size=4096,
        connection=None
    ):
        self.host = host
        self.port = port
        self.database = database
        self.queue = queue
        self.average_message_size = average_message_size
        self.queue_size = queue_size
        self._create_mongo_connection(connection)
        
    def _create_mongo_connection(self, connection):
        if connection:
            self.connection = connection
        else:
            self.connection = pymongo.MongoClient(
                self.host,
                self.port
            )
        self.queue_database = self.connection[self.database]
        self.queue_collection = self.queue_database[self.queue]
        self._create_capped_collection()
        
    def _create_capped_collection(self):
        try:
            pymongo.collection.Collection(
                self.connection[self.database],
                self.queue,
                size = (self.average_message_size * self.queue_size),
                capped = True,
                max = self.queue_size,
                create = True,
                safe = True
            )
             
            self.queue_collection.create_index('_id')
            self.queue_collection.create_index('_meta.routing_key')
            self.queue_collection.create_index('_meta.expired')
            self.queue_collection.create_index('_meta.visible_after')
            
        except pymongo.errors.OperationFailure, operation_failure:
            if not self.ALREADY_EXISTS_EXCEPTION_STRING in str(operation_failure):
                raise operation_failure
    
    def write(self, message, routing_key=None, expire=-1.0, unique_key=None,
                                            visibility_timeout=None,
                   queue_len_threshold=None, queue_len_check_probability=None):

        if (queue_len_threshold is not None and
                (queue_len_check_probability is None or
                         random() < queue_len_check_probability)):
            self._wait_until_queue_len_is_lte(queue_len_threshold,
                                               routing_key=routing_key)

        if type(message) == dict:
            message_dict = message
        else:
            message_dict = message.to_dictionary()
            
        message_dict['_meta'] = {}
        message_dict['_meta']['expired'] = False
        message_dict['_meta']['timestamp'] = time.time()
        message_dict['_meta']['expire'] = expire
        message_dict['_meta']['visible_after'] = (-1.0 if visibility_timeout is None
                                             else time.time() + visibility_timeout)
        
        if routing_key:
            message_dict['_meta']['routing_key'] = routing_key
        
        if not unique_key:
            self.queue_collection.insert(message_dict, safe=True)
            return True
        else:
            return self._unique_insert(message_dict, unique_key)
    
    def _unique_insert(self, message_dict, unique_key):
        return self.queue_database.eval(
            Code(
                "function(obj) { if ( db.%s.count({%s: obj.%s}) ) { return false; } db.%s.insert(obj); return true;}" % (
                    self.queue,
                    unique_key,
                    unique_key,
                    self.queue
            )
        ), message_dict)
    
    def read(self, routing_key=None, messages_read=10, visibility_timeout=-1.0, block=False, polling_interval=1.0, polling_timeout=None):
        messages = []
        current_time = time.time()
        query = {
            '_meta.expired': False,
            '_meta.visible_after': {
              '$lt': current_time
            }
        }
        if routing_key:
            query['_meta.routing_key'] = routing_key
        else:
            query['_meta.routing_key'] = {
                '$exists': False
            }
        
        update = {}
        if visibility_timeout > -1.0:
            update = {
                "$set": {
                    "_meta.visible_after": current_time + visibility_timeout
                }
            }
        
        raw_messages=[]
        
        if block:
            self._block_until_message_available(query, polling_interval, polling_timeout)
        
        if update:        
            for i in range(0, messages_read):
                raw_message = self.queue_collection.find_and_modify(query=query, update=update)
                if raw_message:
                    raw_messages.append(raw_message)
        else:
            for raw_message in self.queue_collection.find(query).limit(messages_read):
                raw_messages.append(raw_message)
                
        for raw_message in raw_messages:
            message = Message(dictionary=raw_message, queue_collection=self.queue_collection)
            
            if not message.is_expired():
                messages.append(message)
            
        return messages
    
    def _block_until_message_available(self, query, polling_interval=1.0, polling_timeout=None):
        current_time = time.time()
        while not self.queue_collection.find(query):
            if polling_timeout and (time.time() - current_time) > polling_timeout:
                break
            time.sleep(polling_interval)
    
    def delete_messages(self, messages):
        ids = []
        for message in messages:
            ids.append(message._source['_id'])
        
        self.queue_collection.update(
            {
                '_id': {
                    '$in': ids
                }
            },
            {
                '$set': {
                    '_meta.expired': True
                }
            },
            multi=True,
            safe=True
        )

    def _wait_until_queue_len_is_lte(self, queue_len_threshold, routing_key=None,
                                     polling_interval=1.0, polling_timeout=None):

        current_time = time.time()
        query = {
            '_meta.expired': False,
            '_meta.visible_after': {
              '$lt': current_time
            }
        }
        if routing_key:
            query['_meta.routing_key'] = routing_key
        else:
            query['_meta.routing_key'] = {
                '$exists': False
            }

        while self.queue_collection.find(query).count() > queue_len_threshold:
            if polling_timeout and (time.time() - current_time) > polling_timeout:
                break
            time.sleep(polling_interval)
