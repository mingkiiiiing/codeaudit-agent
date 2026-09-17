package com.corpus.util;

import com.corpus.dao.AuditLogDao;

/** 干净反例：通配导入 + 类型静态调用（跨文件解析通配口径的正例）。 */
public class SessionLogger {

    private static AuditLogDao audit = AuditLogDao.create();

    public void tick(int n) {
        audit.record("tick:" + n);
    }
}
