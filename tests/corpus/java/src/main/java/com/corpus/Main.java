package com.corpus;

import com.corpus.config.AppConfig;
import com.corpus.dao.UserDao;
import com.corpus.model.User;
import com.corpus.util.MathHelper;

/** 组合入口：跨包类型静态调用 / 实例调用（解析口径的正例集合）。 */
public class Main {

    public static void main(String[] args) {
        String bucket = AppConfig.describe();
        String greeting = MathHelper.greet("audit");
        User guest = new User(1, "guest");
        UserDao dao = new UserDao();
        dao.save(guest.display());
        System.out.println(bucket + "/" + greeting);
    }
}
