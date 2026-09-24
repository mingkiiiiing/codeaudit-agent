// 审计日志 DAO：参数化查询口径（W28-C 语料，干净反例）。
#include <string>
#include "user_dao.hpp"
#include "../util/string_util.hpp"

namespace app {
namespace dao {

int append_audit(const std::string& actor, const std::string& action) {
    std::string actor_key = app::util::to_upper(actor);
    const char* sql = "INSERT INTO audit_log(actor, action) VALUES(?, ?)";
    return bind_and_exec(sql, actor_key, action);
}

std::string last_audit_actor() {
    const char* sql = "SELECT actor FROM audit_log ORDER BY id DESC LIMIT 1";
    return query_scalar(sql);
}

}  // namespace dao
}  // namespace app
