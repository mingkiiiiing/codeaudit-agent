// 字符串工具声明（W28-C 语料，干净反例）。
#ifndef STRING_UTIL_HPP
#define STRING_UTIL_HPP

#include <string>

namespace app {
namespace util {

std::string to_upper(const std::string& raw);
std::string slugify(const std::string& raw);
void normalize(int value);

}  // namespace util
}  // namespace app

#endif  // STRING_UTIL_HPP
