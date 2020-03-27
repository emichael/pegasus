#include <cassert>
#include <sys/time.h>

#include <utils.h>
#include <logger.h>
#include <apps/memcachekv/client.h>
#include <apps/memcachekv/utils.h>

using std::string;

namespace memcachekv {

KVWorkloadGenerator::KVWorkloadGenerator(std::deque<std::string> *keys,
                                         int value_len,
                                         float get_ratio,
                                         float put_ratio,
                                         int mean_interval,
                                         float alpha,
                                         KeyType key_type,
                                         DynamismType d_type,
                                         int d_interval,
                                         int d_nkeys)
    : keys(keys), get_ratio(get_ratio), put_ratio(put_ratio), key_type(key_type),
    d_type(d_type), d_interval(d_interval), d_nkeys(d_nkeys)
{
    this->value = string(value_len, 'v');
    if (key_type == ZIPF) {
        // Generate zipf distribution data
        float c = 0;
        for (unsigned int i = 0; i < keys->size(); i++) {
            c = c + (1.0 / pow((float)(i+1), alpha));
        }
        c = 1 / c;
        float sum = 0;
        for (unsigned int i = 0; i< keys->size(); i++) {
            sum += (c / pow((float)(i+1), alpha));
            this->zipfs.push_back(sum);
        }
    }
    this->unif_real_dist = std::uniform_real_distribution<float>(0.0, 1.0);
    this->unif_int_dist = std::uniform_int_distribution<int>(0, keys->size()-1);
    this->poisson_dist = std::poisson_distribution<int>(mean_interval);
    struct timeval time;
    gettimeofday(&time, nullptr);
    this->generator.seed(time.tv_sec * 1000000 + time.tv_usec);
    this->last_interval = time;
}

int KVWorkloadGenerator::next_zipf_key_index()
{
    float random = 0.0;
    while (random == 0.0) {
        random = this->unif_real_dist(this->generator);
    }

    int l = 0, r = this->keys->size(), mid = 0;
    while (l < r) {
        mid = (l + r) / 2;
        if (random > this->zipfs[mid]) {
            l = mid + 1;
        } else if (random < this->zipfs[mid]) {
            r = mid - 1;
        } else {
            break;
        }
    }
    return mid;
}

Operation::Type KVWorkloadGenerator::next_op_type()
{
    float op_choice = this->unif_real_dist(this->generator);
    Operation::Type op_type;
    if (op_choice < this->get_ratio) {
        op_type = Operation::Type::GET;
    } else if (op_choice < this->get_ratio + this->put_ratio) {
        op_type = Operation::Type::PUT;
    } else {
        op_type = Operation::Type::DEL;
    }
    return op_type;
}

NextOperation KVWorkloadGenerator::next_operation()
{
    if (this->d_type != DynamismType::NONE) {
        struct timeval tv;
        gettimeofday(&tv, nullptr);
        if (latency(this->last_interval, tv) >= this->d_interval) {
            this->last_interval = tv;
            change_keys();
        }
    }

    Operation op;
    switch (this->key_type) {
    case UNIFORM: {
        op.key = this->keys->at(this->unif_int_dist(this->generator));
        break;
    }
    case ZIPF: {
        op.key = this->keys->at(next_zipf_key_index());
        break;
    }
    default:
        panic("Unknown key distribution type");
    }

    op.op_type = next_op_type();
    if (op.op_type == Operation::Type::PUT) {
        op.value = this->value;
    }

    return NextOperation(this->poisson_dist(this->generator), op);
}

void KVWorkloadGenerator::change_keys()
{
    switch (this->d_type) {
    case DynamismType::HOTIN: {
        for (int i = 0; i < this->d_nkeys; i++) {
            this->keys->push_front(this->keys->back());
            this->keys->pop_back();
        }
        break;
    }
    case DynamismType::RANDOM: {
        for (int i = 0; i < this->d_nkeys; i++) {
            int k1 = rand() % 10000;
            int k2 = rand() % this->keys->size();
            std::string tmp = this->keys->at(k1);
            this->keys->at(k1) = this->keys->at(k2);
            this->keys->at(k2) = tmp;
        }
        break;
    }
    default:
        panic("Unknown dynamism type");
    }
}

Client::Client(Configuration *config,
               MemcacheKVStats *stats,
               KVWorkloadGenerator *gen,
               MessageCodec *codec)
    : config(config), stats(stats), gen(gen), codec(codec), req_id(1), phase(WARMUP)
{
}

Client::~Client()
{
}

void Client::receive_message(const string &message, const Address &addr)
{
    MemcacheKVMessage msg;
    this->codec->decode(message, msg);
    assert(msg.type == MemcacheKVMessage::Type::REPLY);
    assert(msg.reply.client_id == this->config->client_id);
    PendingRequest &pending_request = get_pending_request(msg.reply.req_id);

    if (pending_request.op_type == Operation::Type::GET) {
        complete_op(msg.reply.req_id, pending_request, msg.reply.result);
    } else {
        pending_request.received_acks += 1;
        if (pending_request.received_acks >= pending_request.expected_acks) {
            complete_op(msg.reply.req_id, pending_request, msg.reply.result);
        }
    }
}

void Client::run(int duration)
{
    struct timeval start, now;
    gettimeofday(&start, nullptr);
    gettimeofday(&now, nullptr);

    do {
        NextOperation next_op = this->gen->next_operation();
        wait(now, next_op.time);
        execute_op(next_op.op);
        gettimeofday(&now, nullptr);

        switch (this->phase) {
        case WARMUP: {
            if (latency(start, now) > (duration * 200000)) {
                this->phase = RECORD;
                this->stats->start();
            }
            break;
        }
        case RECORD: {
            if (latency(start, now) > (duration * 800000)) {
                this->phase = COOLDOWN;
                this->stats->done();
            }
            break;
        }
        default:
            break;
        }
    } while (latency(start, now) < duration * 1000000);

    this->stats->dump();
}

void Client::execute_op(const Operation &op)
{
    PendingRequest pending_request;
    gettimeofday(&pending_request.start_time, nullptr);
    pending_request.op_type = op.op_type;
    pending_request.expected_acks = 1;
    insert_pending_request(this->req_id, pending_request);

    MemcacheKVMessage msg;
    string msg_str;
    msg.type = MemcacheKVMessage::Type::REQUEST;
    msg.request.client_id = this->config->client_id;
    msg.request.req_id = this->req_id;
    msg.request.node_id = key_to_node_id(op.key, this->config->num_nodes);
    msg.request.op = op;
    this->codec->encode(msg_str, msg);

    // Chain replication: send READs to tail rack and WRITEs to head rack
    int rack_id = op.op_type == Operation::Type::GET ? this->config->num_racks-1 : 0;
    this->transport->send_message_to_node(msg_str, rack_id, msg.request.node_id);

    this->req_id++;
    this->stats->report_issue();
}

void Client::complete_op(uint32_t req_id, const PendingRequest &request, Result result)
{
    struct timeval end_time;
    gettimeofday(&end_time, nullptr);
    this->stats->report_op(request.op_type,
                           latency(request.start_time, end_time),
                           result == Result::OK);
    delete_pending_request(req_id);
}

void Client::insert_pending_request(uint32_t req_id, const PendingRequest &request)
{
    std::lock_guard<std::mutex> lck(this->pending_requests_mutex);
    this->pending_requests[req_id] = request;
}

PendingRequest& Client::get_pending_request(uint32_t req_id)
{
    std::lock_guard<std::mutex> lck(this->pending_requests_mutex);
    assert(this->pending_requests.count(req_id) > 0);
    return this->pending_requests.at(req_id);
}

void Client::delete_pending_request(uint32_t req_id)
{
    std::lock_guard<std::mutex> lck(this->pending_requests_mutex);
    this->pending_requests.erase(req_id);
}

} // namespace memcachekv