package com.corpus.dao;

/** 审计日志数据访问对象：干净反例，提供静态方法供跨文件类型调用解析。 */
public class AuditLogDao {

    private static final String CHANNEL = "audit";

    public void record(String event) {
        append(CHANNEL + ":" + event);
    }

    public static AuditLogDao create() {
        return new AuditLogDao();
    }

    private void append(String line) {
        System.out.println(line);
    }
}
