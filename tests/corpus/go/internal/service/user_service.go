// Package service 用户服务：SQL 注入正例（+ 拼接与 fmt.Sprintf 两种形态各一处）。
package service

import (
	"fmt"

	"internal/dao"
	"internal/util"
)

// UserService 用户业务逻辑。
type UserService struct {
	users *dao.UserDao
	audit *dao.AuditDAO
}

// NewUserService 构造。
func NewUserService(users *dao.UserDao, audit *dao.AuditDAO) *UserService {
	return &UserService{users: users, audit: audit}
}

// Search 拼接构造 SQL（正例 1：字符串 + 拼接）。
func (s *UserService) Search(name string, exec ExecFn) {
	sql := "SELECT * FROM users WHERE name='" + name + "'"
	exec(sql)
	s.audit.Record("search")
}

// Lookup Sprintf 格式化构造 SQL（正例 2：fmt.Sprintf 形态）。
func (s *UserService) Lookup(id string, exec ExecFn) {
	q := fmt.Sprintf("DELETE FROM sessions WHERE token='%s'", id)
	exec(q)
}

// Load 跨包解析正例：dao.FindUser / util.Mask 均按 import 包名解析。
func (s *UserService) Load(id int) string {
	user, ok := s.users.Find(id)
	if !ok {
		return fmt.Sprintf("missing:%d", id)
	}
	return util.Mask(user.Name)
}

// ExecFn SQL 执行函数形态（避免引入真实 sql 包依赖，形态等价）。
type ExecFn func(sql string)
