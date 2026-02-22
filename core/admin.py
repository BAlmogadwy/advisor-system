from django.contrib import admin

from core.models import (
    AcademicAdvisor,
    AuditLog,
    Course,
    Prerequisite,
    ProgrammeRequirement,
    Student,
    StudentCourse,
    StudentTermSection,
    TermSection,
    TermSectionMeeting,
    UserScope,
)

admin.site.register(Student)
admin.site.register(Course)
admin.site.register(StudentCourse)
admin.site.register(ProgrammeRequirement)
admin.site.register(Prerequisite)
admin.site.register(AcademicAdvisor)
admin.site.register(TermSection)
admin.site.register(TermSectionMeeting)
admin.site.register(StudentTermSection)
admin.site.register(UserScope)
admin.site.register(AuditLog)
