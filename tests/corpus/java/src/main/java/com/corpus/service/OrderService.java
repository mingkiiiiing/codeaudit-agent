package com.corpus.service;

import com.corpus.dao.UserDao;

/** SQL 注入正例（执行点形态二）：拼接结果进 prepareStatement；含跨文件实例调用。 */
public class OrderService {

    private UserDao userDao;

    public OrderService(UserDao userDao) {
        this.userDao = userDao;
    }

    public void bind(java.sql.Connection conn, String buyer) {
        java.sql.PreparedStatement ps =
                conn.prepareStatement("INSERT INTO orders(buyer) VALUES('" + buyer + "')");
        ps.executeUpdate();
    }

    public String buyerOf(int orderId) {
        String buyer = userDao.find(orderId).orElse("unknown");
        return buyer.trim();
    }
}
