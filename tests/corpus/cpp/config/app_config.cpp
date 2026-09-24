// AppConfig 类外方法定义与只读访问器（W28-C 语料，干净反例）。
#include "app_config.hpp"

namespace app {
namespace config {

AppConfig::AppConfig() : retries_(0) { }

void AppConfig::load() {
    retries_ = MAX_RETRIES;
}

const std::string& AppConfig::endpoint() const {
    return endpoint_;
}

int AppConfig::retry_limit() { return MAX_RETRIES; }

}  // namespace config
}  // namespace app
