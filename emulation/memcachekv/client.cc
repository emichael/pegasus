#include "utils.h"
#include "logger.h"
#include "memcachekv/client.h"
#include "memcachekv/memcachekv.pb.h"

using std::string;

namespace memcachekv {
using namespace proto;

KVWorkloadGenerator::KVWorkloadGenerator(const std::vector<std::string> *keys,
                                         int value_len,
                                         float get_ratio,
                                         float put_ratio,
                                         int mean_interval,
                                         float alpha,
                                         KeyType key_type)
    : keys(keys), get_ratio(get_ratio), put_ratio(put_ratio), key_type(key_type)
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
}

int
KVWorkloadGenerator::next_zipf_key_index()
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

Operation::Type
KVWorkloadGenerator::next_op_type()
{
    float op_choice = this->unif_real_dist(this->generator);
    Operation::Type op_type;
    if (op_choice < this->get_ratio) {
        op_type = Operation_Type_GET;
    } else if (op_choice < this->get_ratio + this->put_ratio) {
        op_type = Operation_Type_PUT;
    } else {
        op_type = Operation_Type_DEL;
    }
    return op_type;
}

NextOperation
KVWorkloadGenerator::next_operation()
{
    Operation op;
    switch (this->key_type) {
    case UNIFORM: {
        op.set_key(this->keys->at(this->unif_int_dist(this->generator)));
        break;
    }
    case ZIPF: {
        op.set_key(this->keys->at(next_zipf_key_index()));
        break;
    }
    default:
        panic("Unknown key distribution type");
    }

    Operation::Type op_type = next_op_type();
    op.set_op_type(op_type);
    if (op_type == Operation_Type_PUT) {
        op.set_value(this->value);
    }

    return NextOperation(this->poisson_dist(this->generator), op);
}

Client::Client(Transport *transport,
               MemcacheKVStats *stats,
               KVWorkloadGenerator *gen)
    : transport(transport), stats(stats), gen(gen), req_id(1) {}

void
Client::receive_message(const string &message, const sockaddr &src_addr)
{
    MemcacheKVReply reply;
    reply.ParseFromString(message);
    assert(this->pending_requests.count(reply.req_id()) > 0);
    PendingRequest &pending_request = this->pending_requests.at(reply.req_id());

    if (pending_request.op_type == Operation_Type_GET) {
        complete_op(reply.req_id(), reply.result());
    } else {
        pending_request.received_acks += 1;
        if (pending_request.received_acks >= pending_request.expected_acks) {
            complete_op(reply.req_id(), reply.result());
        }
    }
}

void
Client::run(int duration)
{
    struct timeval start, now;
    gettimeofday(&start, nullptr);

    this->stats->start();
    do {
        NextOperation next_op = this->gen->next_operation();
        wait(next_op.time);
        execute_op(next_op.op);
        gettimeofday(&now, nullptr);
    } while (latency(start, now) / 1000000 < duration);

    this->stats->done();
    this->stats->dump();
}

void
Client::execute_op(const proto::Operation &op)
{
    PendingRequest pending_request;
    gettimeofday(&pending_request.start_time, nullptr);
    pending_request.op_type = op.op_type();
    pending_request.expected_acks = 1;
    this->pending_requests[this->req_id] = pending_request;

    MemcacheKVRequest request;
    string request_str;
    request.set_req_id(this->req_id);
    *(request.mutable_op()) = op;
    request.SerializeToString(&request_str);
    this->transport->send_message_to_node(request_str, 0);

    this->req_id++;
}

void
Client::complete_op(uint64_t req_id, Result result)
{
    PendingRequest &pending_request = this->pending_requests.at(req_id);
    struct timeval end_time;
    gettimeofday(&end_time, nullptr);
    this->stats->report_op(pending_request.op_type,
                           latency(pending_request.start_time, end_time),
                           result == Result::OK);
    this->pending_requests.erase(req_id);
}

} // namespace memcachekv