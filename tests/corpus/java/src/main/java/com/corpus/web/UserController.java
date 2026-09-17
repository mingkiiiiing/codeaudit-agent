package com.corpus.web;

import com.corpus.dao.UserDao;
import com.corpus.service.UserService;

/** Web 层干净反例：注释含 SQL 字样不应命中；含跨文件实例调用。 */
public class UserController {

    private static final String PAGE_TITLE = "user-admin";
    private UserDao userDao;
    private UserService userService;

    public UserController(UserDao userDao, UserService userService) {
        this.userDao = userDao;
        this.userService = userService;
    }

    /**
     * 渲染用户页。底层查询为 SELECT * FROM users WHERE name=?（参数化，非拼接）。
     */
    public String render(int uid) {
        String who = userService.load(uid);
        String avatar = userDao.find(uid).orElse("guest");
        return "<html><title>" + PAGE_TITLE + "</title><body>" + who + "/" + avatar + "</body></html>";
    }
}
