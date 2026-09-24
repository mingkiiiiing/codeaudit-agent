// 部署密钥配置（W28-C 语料，正例：硬编码密钥三形态）。
#include "app_config.hpp"

#define DEPLOY_TOKEN "sk-aX9kQ2vL8mN4pR7sT5uW3yZ0"

namespace app {
namespace config {

const std::string kApiSecret = "aX9kQ2vL8mN4pR7sT5uW3yZ1";
std::string db_password = "mysupersecretkey";

const std::string& deploy_token() {
    static const std::string kToken = DEPLOY_TOKEN;
    return kToken;
}

}  // namespace config
}  // namespace app
