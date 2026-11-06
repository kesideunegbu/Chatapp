import socket
import threading
from enum import Enum
import json

# Server IP and Port configuration
HOST = '127.0.0.1'  # Server IP (localhost)
PORT = 65432        # Port for client connections

clients = {}
channels = {} # TODO: Do we include defaults? 

def handle_client_request(conn, addr, data):
  pass

# Function to manage client connections
def handle_client(conn, addr):
    print(f"Connection established with {addr}.")
    while True:
        try:
            data = conn.recv(1024)  # Receive data from the client
            if not data:
                break  # Terminate the connection if no data is received
            print(f"Received from {addr}: {data.decode()}")
            handle_client_request(conn, addr, data)
            conn.sendall(f"Server response: {data.decode()}".encode())  # Send the received data back to the client
        except ConnectionResetError:
            print(f"Client {addr} has disconnected.")
            break
    conn.close()
    print(f"Connection with {addr} closed.")

# Start the server
def start_server():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server_socket:
        server_socket.bind((HOST, PORT))
        server_socket.listen()
        print(f"Server listening on {HOST}:{PORT}...")
        
        while True:
            conn, addr = server_socket.accept()  # Accept a client connection
            thread = threading.Thread(target=handle_client, args=(conn, addr))
            thread.start()  # Start a new thread for each client
            print(f"Active connections: {threading.active_count() - 1}")
if __name__ == "__main__":
    start_server()
