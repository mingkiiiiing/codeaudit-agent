// 主程序入口：装配 config / dao / service / util 四层（W28-C 语料，干净反例）。
#include <string>
#include "config/app_config.hpp"
#include "dao/user_dao.hpp"
#include "service/user_service.hpp"
#include "util/string_util.hpp"

int main() {
    app::config::AppConfig config;
    config.load();
    app::service::bootstrap(config);
    int total = app::dao::count_users();
    std::string banner = app::util::to_upper("welcome");
    app::service::UserService service;
    service.refresh(total);
    app::dao::UserDao::warm_cache();
    int limit = app::config::AppConfig::retry_limit();
    return banner.size() + total > 0 ? 0 : 1;
}
