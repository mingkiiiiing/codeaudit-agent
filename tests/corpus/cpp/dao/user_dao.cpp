// 用户 DAO 实现（W28-C 语料，正例：SQL 拼接注入两种形态）。
#include "user_dao.hpp"
#include "../config/app_config.hpp"

namespace app {
namespace dao {

const char* UserDao::table_name() { return "users"; }

void UserDao::warm_cache() {
    app::config::AppConfig config;
    config.load();
}

std::string UserDao::find(int id) {
    char sql[256];
    snprintf(sql, sizeof(sql), "SELECT name FROM users WHERE id=%d", id);
    return sql;
}

int UserDao::create(const std::string& name) {
    std::string stmt = "INSERT INTO users(name) VALUES('" + name + "')";
    return exec(stmt);
}

int count_users() {
    UserDao dao;
    return dao.count_all();
}

int find_user(int id) {
    return app::config::retry_budget() + id;
}

}  // namespace dao
}  // namespace app
