// 用户 DAO 类声明（W28-C 语料，干净反例）。
#ifndef USER_DAO_HPP
#define USER_DAO_HPP

#include <string>

namespace app {
namespace dao {

class UserDao {
public:
    static const char* table_name();
    static void warm_cache();
    std::string find(int id);
    int create(const std::string& name);

private:
    int last_id_;
};

int count_users();
int find_user(int id);

}  // namespace dao
}  // namespace app

#endif  // USER_DAO_HPP
