#ifndef _CONFIGURATION_H_
#define _CONFIGURATION_H_

#include <vector>
#include <string>

class Address {
public:
    virtual ~Address() = 0;
};

class Configuration {
public:
    enum NodeType {
        CLIENT,
        SERVER,
        LB
    };

    Configuration();
    virtual ~Configuration() = 0;

    const Address *my_address() const;

    int duration;
    int num_racks;
    int num_nodes;
    int rack_id;
    int node_id;
    int client_id;
    int transport_core;
    int n_transport_threads;
    int app_core;
    int n_app_threads;
    int colocate_id;
    int n_colocate_nodes;
    NodeType node_type;
    bool terminating;
    bool use_raw_transport;
    bool use_endhost_lb;
    std::vector<std::vector<Address*>> node_addresses;
    std::vector<Address*> client_addresses;
    Address *lb_address;
    std::vector<Address*> controller_addresses;
};

#endif /* _CONFIGURATION_H_ */
