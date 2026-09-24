// Package web 用户控制器：干净反例；演示 func_literal 边界。
package web

import (
	"fmt"

	"internal/service"
)

// UserController 用户路由控制器。
type UserController struct {
	users *service.UserService
}

// NewUserController 构造。
func NewUserController(users *service.UserService) *UserController {
	return &UserController{users: users}
}

// HandleSearch 查询用户（跨包解析正例：service 包名绑定）。
func (c *UserController) HandleSearch(name string, exec func(string)) {
	c.users.Search(name, exec)
}

// ReportName 报表名（跨包解析正例：包级构造函数 service.NewReportService）。
func (c *UserController) ReportName() string {
	return service.NewReportService("web").Title("users")
}

// HandleAsync 异步预取（已知边界演示：go func 字面量体内的调用不收集，
// 字面量调用本身无 callee 名也不收集；具名调用 c.warm() 正常收集）。
func (c *UserController) HandleAsync() {
	c.warm()
	go func() {
		fmt.Println("warm done")
	}()
}

// warm 同文件方法（同文件调用不建边）。
func (c *UserController) warm() {
	fmt.Println("warm")
}
