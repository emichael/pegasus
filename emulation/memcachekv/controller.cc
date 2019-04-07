#include "memcachekv/controller.h"

namespace memcachekv {

Controller::Controller(Configuration *config,
                       const ControllerMessage &msg)
    : config(config), msg(msg) {}


void
Controller::receive_message(const std::string &message, const sockaddr &src_addr)
{
    ControllerMessage msg;
    if (!this->codec.decode(message, msg)) {
        return;
    }
    if (msg.type != ControllerMessage::Type::RESET_REPLY) {
        return;
    }
    if (msg.reset_reply.ack == Ack::OK) {
        std::unique_lock<std::mutex> lck(mtx);
        this->replied = true;
        this->cv.notify_all();
    }
}

void
Controller::run(int duration)
{
    // Just send one message to the controller
    this->replied = false;
    std::string msg_str;
    this->codec.encode(msg_str, this->msg);
    this->transport->send_message_to_addr(msg_str, this->config->controller_address);
    // Wait for ack
    /*
    std::unique_lock<std::mutex> lck(mtx);
    while (!this->replied) {
        this->cv.wait(lck);
    }
    */
}

} // namespace memcachekv
