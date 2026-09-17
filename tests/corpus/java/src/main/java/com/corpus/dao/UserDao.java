package com.corpus.dao;

import java.util.Optional;

/**
 * 用户数据访问对象：干净反例（无注入/密钥/长函数）。
 * 文档里出现 SELECT * FROM users 字样不应被 SQL 规则命中（注释已掩码）。
 */
public class UserDao {

    public Optional<String> find(int id) {
        // 查询语句由调用方以参数化形式传入，本类只做映射
        if (id <= 0) {
            return Optional.empty();
        }
        return Optional.of("user-" + id);
    }

    public void save(String name) {
        writeLog(name);
    }

    private void writeLog(String msg) {
        System.out.println(msg);
    }
}
