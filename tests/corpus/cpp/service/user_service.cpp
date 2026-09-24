// 用户服务实现（W28-C 语料，正例：超长函数 >80 行）。
#include "user_service.hpp"
#include "../dao/user_dao.hpp"
#include "../util/string_util.hpp"

namespace app {
namespace service {

UserService::UserService() : total_(0) { }

void UserService::refresh(int total) {
    total_ = total;
}

void UserService::sync_all(int days) {
    int total = 0;
    int step = days > 0 ? days : 1;
    total += step * 1;
    total += step * 2;
    total += step * 3;
    total += step * 4;
    total += step * 5;
    total += step * 6;
    total += step * 7;
    total += step * 8;
    total += step * 9;
    total += step * 10;
    total += step * 11;
    total += step * 12;
    total += step * 13;
    total += step * 14;
    total += step * 15;
    total += step * 16;
    total += step * 17;
    total += step * 18;
    total += step * 19;
    total += step * 20;
    total += step * 21;
    total += step * 22;
    total += step * 23;
    total += step * 24;
    total += step * 25;
    total += step * 26;
    total += step * 27;
    total += step * 28;
    total += step * 29;
    total += step * 30;
    total += step * 31;
    total += step * 32;
    total += step * 33;
    total += step * 34;
    total += step * 35;
    total += step * 36;
    total += step * 37;
    total += step * 38;
    total += step * 39;
    total += step * 40;
    total += step * 41;
    total += step * 42;
    total += step * 43;
    total += step * 44;
    total += step * 45;
    total += step * 46;
    total += step * 47;
    total += step * 48;
    total += step * 49;
    total += step * 50;
    total += step * 51;
    total += step * 52;
    total += step * 53;
    total += step * 54;
    total += step * 55;
    total += step * 56;
    total += step * 57;
    total += step * 58;
    total += step * 59;
    total += step * 60;
    total += step * 61;
    total += step * 62;
    total += step * 63;
    total += step * 64;
    total += step * 65;
    total += step * 66;
    total += step * 67;
    total += step * 68;
    total += step * 69;
    total += step * 70;
    total += step * 71;
    total += step * 72;
    total += step * 73;
    total += step * 74;
    total += step * 75;
    total += step * 76;
    total += step * 77;
    total += step * 78;
    total += step * 79;
    total += step * 80;
    total += step * 81;
    total += step * 82;
    total += step * 83;
    total += step * 84;
    app::util::normalize(total);
    total_ = total;
}

void bootstrap(const app::config::AppConfig& config) {
    config.load();
}

int tally(int seed) {
    return app::dao::find_user(seed);
}

}  // namespace service
}  // namespace app
