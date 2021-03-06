#ifndef _MEMCACHEKV_CLIENT_H_
#define _MEMCACHEKV_CLIENT_H_

#include <string>
#include <vector>
#include <deque>
#include <sys/time.h>
#include <unordered_map>
#include <random>
#include <mutex>

#include <application.h>
#include <configuration.h>
#include <apps/memcachekv/stats.h>
#include <apps/memcachekv/message.h>

namespace memcachekv {

enum class KeyType {
    UNIFORM,
    ZIPF
};

enum class SendMode {
    FIXED,
    DYNAMIC
};

enum class DynamismType {
    NONE,
    HOTIN,
    RANDOM
};

class KVWorkloadGenerator {
public:
    KVWorkloadGenerator(std::deque<std::string> &keys,
                        int value_len,
                        float get_ratio,
                        float put_ratio,
                        float mean_interval,
                        int target_latency,
                        float alpha,
                        KeyType key_type,
                        SendMode send_mode,
                        DynamismType d_type,
                        int d_interval,
                        int d_nkeys,
                        int n_threads,
                        Stats *stats);
    ~KVWorkloadGenerator();

    void next_operation(int tid, Operation &op, long &time);

private:
    int next_zipf_key_index(int tid);
    OpType next_op_type(int tid);
    void change_keys();
    void adjust_send_rate(int tid);

    std::deque<std::string> &keys;
    float get_ratio;
    float put_ratio;
    int target_latency;
    KeyType key_type;
    SendMode send_mode;
    DynamismType d_type;
    int d_interval;
    int d_nkeys;
    Stats *stats;

    std::string value;
    std::vector<float> zipfs;
    struct timeval last_interval;

    // Per thread
    class ThreadState {
    public:
        ThreadState();

        uint64_t op_count;
        long mean_interval;
        std::default_random_engine generator;
        std::uniform_real_distribution<float> unif_real_dist;
        std::uniform_int_distribution<int> unif_int_dist;
        std::poisson_distribution<long> poisson_dist;
    };
    std::vector<ThreadState> thread_states;
};

class Client : public Application {
public:
    Client(Configuration *config,
           Stats *stats,
           KVWorkloadGenerator *gen,
           MessageCodec *codec);
    ~Client();

    virtual void receive_message(const Message &msg,
                                 const Address &addr,
                                 int tid) override final;
    virtual void run() override final;
    virtual void run_thread(int tid) override final;

private:
    void execute_op(const MemcacheKVMessage &kvmsg);
    void complete_op(int tid, const MemcacheKVReply &reply);

    Configuration *config;
    Stats *stats;
    KVWorkloadGenerator *gen;
    MessageCodec *codec;
};

} // namespace memcachekv

#endif /* _MEMCACHEKV_CLIENT_H_ */
