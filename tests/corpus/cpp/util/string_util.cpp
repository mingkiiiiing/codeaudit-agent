// 字符串工具实现（W28-C 语料，干净反例：顶层函数与限定/成员调用形态）。
#include "string_util.hpp"
#include <cctype>

namespace app {
namespace util {

std::string to_upper(const std::string& raw) {
    std::string out = raw;
    for (char& ch : out) {
        ch = static_cast<char>(std::toupper(static_cast<unsigned char>(ch)));
    }
    return out;
}

std::string slugify(const std::string& raw) {
    std::string out;
    for (char ch : raw) {
        if (ch == ' ') {
            out.push_back('-');
        } else {
            out.push_back(ch);
        }
    }
    return out;
}

void normalize(int value) {
    if (value < 0) {
        value = 0;
    }
}

void deferred_log(const std::string& msg) {
    auto task = [&msg]() {
        audit::write(msg);
    };
    task();
}

int clamp_box(int v) {
    auto boxed = make_box<int>(v);
    return boxed.value();
}

}  // namespace util
}  // namespace app
