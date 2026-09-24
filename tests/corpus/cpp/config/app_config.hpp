// 应用配置常量与配置类声明（W28-C 语料，干净反例）。
#ifndef APP_CONFIG_HPP
#define APP_CONFIG_HPP

#include <string>

#define APP_NAME "codeaudit-cpp-corpus"
#define MAX_RETRIES 5

namespace app {
namespace config {

constexpr int kTimeoutSeconds = 30;
const double kSuccessRatio = 0.99;

class AppConfig {
public:
    AppConfig();
    void load();
    const std::string& endpoint() const;
    static int retry_limit();

private:
    std::string endpoint_;
    int retries_;
};

}  // namespace config
}  // namespace app

#endif  // APP_CONFIG_HPP
