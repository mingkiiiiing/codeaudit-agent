// Package model 领域模型：干净反例。
package model

import "fmt"

// User 用户实体（struct 声明 → class 口径，name 为包内简名）。
type User struct {
	ID   int
	Name string
}

// New 构造函数。
func New(id int, name string) User {
	return User{ID: id, Name: name}
}

// Display 值接收者方法（UserDao.Label 同款限定口径：User.Display）。
func (u User) Display() string {
	return fmt.Sprintf("user:%d:%s", u.ID, u.Name)
}

// FromRow 行数据映射（audit_dao 跨包调用的目标符号）。
func FromRow(raw string) User {
	return User{Name: raw}
}
