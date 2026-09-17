package com.corpus.service;

import com.corpus.dao.AuditLogDao;
import com.corpus.dao.UserDao;

/** SQL 注入正例：字符串拼接 SQL 直接进 executeQuery。 */
public class UserService {

    private UserDao userDao;
    private AuditLogDao auditLog;

    public UserService(UserDao userDao, AuditLogDao auditLog) {
        this.userDao = userDao;
        this.auditLog = auditLog;
    }

    public void search(String name) {
        String sql = "SELECT * FROM users WHERE name='" + name + "'";
        execute(sql);
    }

    public void lookup(String id) {
        java.sql.Statement st = open();
        st.executeQuery("SELECT id, name FROM users WHERE id=" + id);
        auditLog.record("lookup");
    }

    public String load(int id) {
        String found = userDao.find(id).orElse("anonymous");
        auditLog.record("load:" + found);
        return found;
    }

    private void execute(String sql) {
        System.out.println(sql);
    }

    private java.sql.Statement open() {
        return null;
    }
}
