#ifndef __MEMCACHEKV_CLIENT_H__
#define __MEMCACHEKV_CLIENT_H__

#include <string>
#include <vector>
#include <sys/time.h>
#include <unordered_map>
#include <random>
#include "application.h"
#include "memcachekv/stats.h"
#include "memcachekv/memcachekv.pb.h"

namespace memcachekv {

struct NextOperation {
    inline NextOperation(int time, const proto::Operation &op)
        : time(time), op(op) {};
    int time;
    proto::Operation op;
};

enum KeyType {
    UNIFORM,
    ZIPF
};

class KVWorkloadGenerator {
public:
    KVWorkloadGenerator(const std::vector<std::string> *keys,
                        int value_len,
                        float get_ratio,
                        float put_ratio,
                        int mean_interval,
                        float alpha,
                        KeyType key_type);
    ~KVWorkloadGenerator() {};

    NextOperation next_operation();
private:
    int next_zipf_key_index();
    proto::Operation::Type next_op_type();

    const std::vector<std::string> *keys;
    float get_ratio;
    float put_ratio;
    KeyType key_type;
    std::string value;
    std::vector<float> zipfs;
    std::default_random_engine generator;
    std::uniform_real_distribution<float> unif_real_dist;
    std::uniform_int_distribution<int> unif_int_dist;
    std::poisson_distribution<int> poisson_dist;
};

struct PendingRequest {
    proto::Operation_Type op_type;
    struct timeval start_time;
    int received_acks;
    int expected_acks;

    inline PendingRequest()
        : op_type(proto::Operation_Type_GET),
        received_acks(0),
        expected_acks(0) {};
};

class Client : public Application {
public:
    Client(Transport *transport,
           MemcacheKVStats *stats,
           KVWorkloadGenerator *gen);
    ~Client() {};

    void receive_message(const std::string &message,
                         const sockaddr &src_addr) override;
    void run(int duration) override;

private:
    void execute_op(const proto::Operation &op);
    void complete_op(uint64_t req_id, proto::Result result);

    Transport *transport;
    MemcacheKVStats *stats;
    KVWorkloadGenerator *gen;

    uint64_t req_id;
    std::unordered_map<uint64_t, PendingRequest> pending_requests;
};

} // namespace memcachekv

#endif /* __MEMCACHEKV_CLIENT_H__ */