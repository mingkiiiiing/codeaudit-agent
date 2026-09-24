// Package dao 用户数据访问对象：干净反例（无注入/密钥/长函数）。
// 文档里出现 SELECT * FROM users 字样不应被 SQL 规则命中（注释已掩码）。
package dao

import "internal/model"

// UserDao 用户表访问（指针接收者方法 → (*UserDao).Method 限定口径）。
type UserDao struct {
	table string
}

// NewUserDao 构造函数（包级函数，简名口径）。
func NewUserDao(table string) *UserDao {
	if table == "" {
		table = "users"
	}
	return &UserDao{table: table}
}

// Find 按主键查用户；参数化写法（反例：不拼接 SQL）。
func (u *UserDao) Find(id int) (model.User, bool) {
	if id <= 0 {
		return model.User{}, false
	}
	return model.New(id, "user"), true
}

// Label 值接收者方法 → UserDao.Label 限定口径。
func (u UserDao) Label() string {
	return u.table + ":ro"
}
