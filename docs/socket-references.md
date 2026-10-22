# Using Sockets
---

### Overview

To facilitate real-time, multi-user communication, our project will use 
sockets, programmed in the Python programming language. Sockets allow
two devices conencted to a network to communicate, transfer, and 
exchange data across the network by combining the ip information
and port number of the devices. The structure our application will
include the ability for a host to send data (a private message or 
file; one-to-one) to another host, or send to a channel (a known group
of hosts; one-to-many). 

### General Socket Understanding

- Network sockets allow for data transfer and communication between devices
via the ip address and port number.
- The Python socket API provides a set of functions that that are implemented
as wrappers around their respective system calls in C.
- Sockets can operate over TCP or UDP dependent on use case and developer choice.

### References

For more detailed information regarding network sockets and their implementation
in python, refer to the links below.

- https://medium.com/@denizhalil/advanced-socket-programming-with-python-multi-client-and-server-communication-c0416836c3bd
- https://realpython.com/python-sockets/
- https://dev.to/pubnub/socket-programming-in-python-client-server-and-peer-examples-5bkl
