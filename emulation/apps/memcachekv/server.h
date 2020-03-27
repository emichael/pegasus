#ifndef __MEMCACHEKV_SERVER_H__
#define __MEMCACHEKV_SERVER_H__

#include <string>
#include <unordered_map>
#include <set>
#include <mutex>
#include <vector>
#include <tbb/concurrent_unordered_map.h>

#include <application.h>
#include <apps/memcachekv/message.h>

namespace memcachekv {

class Server : public Application {
public:
    Server(Configuration *config, MessageCodec *codec,
           ControllerCodec *ctrl_codec, int proc_latency,
           std::string default_value, bool report_load);
    ~Server();

    virtual void receive_message(const std::string &message,
                                 const Address &addr) override final;
    virtual void run(int duration) override final;

private:
    void process_kv_message(const MemcacheKVMessage &msg,
                            const Address &addr);
    void process_ctrl_message(const ControllerMessage &msg,
                              const Address &addr);
    void process_kv_request(const MemcacheKVRequest &request,
                            const Address &addr);
    void process_op(const Operation &op,
                    MemcacheKVReply &reply);
    void process_migration_request(const MigrationRequest &request);
    void process_ctrl_key_migration(const ControllerKeyMigration &key_mgr);
    void update_rate(const Operation &op);
    load_t calculate_load();

    Configuration *config;
    MessageCodec *codec;
    ControllerCodec *ctrl_codec;

    struct Item {
        Item()
            : value(""), ver(0) {};
        Item(const std::string &value, ver_t ver)
            : value(value), ver(ver) {};
        std::string value;
        ver_t ver;
    };
    tbb::concurrent_unordered_map<std::string, Item> store;

    struct ClientTableEntry {
        ClientTableEntry()
            : req_id(0), msg("") {};
        ClientTableEntry(uint32_t req_id, const std::string &msg)
            : req_id(req_id), msg(msg) {};
        uint32_t req_id;
        std::string msg;
    };
    tbb::concurrent_unordered_map<int, ClientTableEntry> client_table; // client id -> table entry

    int proc_latency;
    std::string default_value;
    bool report_load;
    /* Load related */
    static const int EPOCH_DURATION = 1000; // 1ms
    struct timeval epoch_start;
    std::mutex load_mutex;
    std::list<struct timeval> request_ts;

    static const int HK_EPOCH = 10000; // 10ms
    static const int MAX_HK_SIZE = 8;
    static const int KR_SAMPLE_RATE = 100;
    static const int HK_THRESHOLD = 5;
    unsigned int request_count;
    std::unordered_map<keyhash_t, unsigned int> key_count;
    std::unordered_map<keyhash_t, unsigned int> hk_report;
    std::mutex hk_mutex;
};

} // namespace memcachekv

#endif /* __MEMCACHEKV_SERVER_H__ */