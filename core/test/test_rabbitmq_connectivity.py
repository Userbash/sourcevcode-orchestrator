import pika
import unittest
import time
import os

class TestRabbitMQConnectivity(unittest.TestCase):
    def setUp(self):
        self.url = os.getenv("RABBITMQ_URL", "amqp://guest:guest@localhost:5672/")
        
    def test_connection_and_queue(self):
        """Проверка соединения и возможности создания очереди."""
        connection = pika.BlockingConnection(pika.URLParameters(self.url))
        channel = connection.channel()
        
        queue_name = 'test_health_queue'
        channel.queue_declare(queue=queue_name, durable=True)
        
        # Publish
        channel.basic_publish(exchange='', routing_key=queue_name, body='health_check')
        
        # Consume
        method, properties, body = channel.basic_get(queue=queue_name, auto_ack=True)
        
        self.assertEqual(body.decode(), 'health_check')
        
        channel.queue_delete(queue=queue_name)
        connection.close()

if __name__ == '__main__':
    unittest.main()
