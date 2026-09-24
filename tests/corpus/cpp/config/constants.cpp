// 模块级数值常量与只读取值函数（W28-C 语料，干净反例）。
#include "app_config.hpp"

#define POOL_SIZE 16
#define VERSION_TAG "1.4.2"

namespace app {
namespace config {

const int kPoolSize = POOL_SIZE;
constexpr double kTimeoutMs = 1500.0;
static const char* kVersion = VERSION_TAG;

int pool_size() { return kPoolSize; }

int retry_budget() { return MAX_RETRIES * 2; }

const char* version_tag() { return VERSION_TAG; }

int pool_doubled() { return config::retry_budget() * 2; }

}  // namespace config
}  // namespace app
