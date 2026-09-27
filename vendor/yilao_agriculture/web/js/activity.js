// 动作只能来自实际确认；不按农活名称、疾病或年龄自动推断。
export const activityActions=[
  {value:'bending',label:'弯腰'},
  {value:'squatting',label:'蹲姿'},
  {value:'lifting',label:'搬抬'},
  {value:'carrying',label:'负重搬运'},
  {value:'prolonged_standing',label:'久站'},
  {value:'climbing',label:'登高'},
  {value:'powered_machinery',label:'动力农机'},
  {value:'pesticide_exposure',label:'接触农药'},
];
export const activityCodes=new Set(activityActions.map(a=>a.value));
