// 用户服务类声明（W28-C 语料，干净反例）。
#ifndef USER_SERVICE_HPP
#define USER_SERVICE_HPP

#include "../config/app_config.hpp"

namespace app {
namespace service {

class UserService {
public:
    UserService();
    void refresh(int total);
    void sync_all(int days);

private:
    int total_;
};

void bootstrap(const app::config::AppConfig& config);
int tally(int seed);

}  // namespace service
}  // namespace app

#endif  // USER_SERVICE_HPP
