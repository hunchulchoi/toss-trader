const { expect, test } = require('@playwright/test');

test('Rule과 Hermes paper 장부를 독립 탐색한다', async ({ page }) => {
  const response = await page.goto('/');
  expect(response.status()).toBe(200);

  await expect(page.getByTestId('timeline-app')).toBeVisible();
  await expect(page.locator('.date-item')).toHaveCount(2);
  await expect(page.getByTestId('selected-date')).toContainText('8월 14일');
  await expect(page.getByTestId('equity-chart').locator('svg')).toBeVisible();
  await expect(page.locator('#positions-body')).toContainText('삼성전자');
  await expect(page.locator('#positions-body .sparkline')).toHaveCount(1);

  await page.getByRole('tab', { name: 'HERMES' }).click();
  await expect(page.locator('#positions-body')).toContainText('SK하이닉스');
  await expect(page.locator('#positions-body')).not.toContainText('삼성전자');
  await expect(page.locator('#selected-caption')).toContainText('Hermes paper 장부');

  await page.getByRole('button', { name: '이전 거래일' }).click();
  await expect(page.getByTestId('selected-date')).toContainText('8월 13일');
  await expect(page.locator('#trade-list .trade-item')).toHaveCount(1);
});

test('모바일에서도 포트폴리오 전환과 날짜 레일이 보인다', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/');

  await expect(page.locator('.date-list')).toBeVisible();
  await expect(page.getByRole('tab', { name: 'RULE' })).toBeVisible();
  await expect(page.getByRole('tab', { name: 'HERMES' })).toBeVisible();
  await expect(page.getByText('READ ONLY')).toBeVisible();
});
