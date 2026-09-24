// 报表服务实现（W28-C 语料，干净反例）。
#include <string>
#include "../config/app_config.hpp"
#include "../util/string_util.hpp"

namespace app {
namespace service {

std::string render_daily(int rows) {
    std::string title = app::util::to_upper("daily report");
    std::string slug = app::util::slugify(title);
    return title + std::to_string(rows);
}

std::string report_name() {
    return app::config::version_tag();
}

}  // namespace service
}  // namespace app
