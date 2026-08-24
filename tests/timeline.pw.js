const { expect, test } = require('@playwright/test');

test('Rule과 Hermes paper 장부를 독립 탐색한다', async ({ page }) => {
  const response = await page.goto('/');
  expect(response.status()).toBe(200);

  await expect(page.getByTestId('timeline-app')).toBeVisible();
  await expect(page.getByRole('link', { name: 'CYCLES' })).toHaveAttribute('href', '/cycles');
  await expect(page.locator('a.topbar-link[href="/hermes"]')).toHaveCount(1);
  await expect(page.locator('.date-item')).toHaveCount(2);
  await expect(page.getByTestId('selected-date')).toContainText('8월 14일');
  await expect(page.getByTestId('equity-chart').locator('svg')).toBeVisible();
  await expect(page.locator('#positions-body')).toContainText('삼성전자');
  await expect(page.locator('#positions-body .sparkline')).toHaveCount(1);
  await expect(page.getByTestId('hunter-shadow')).toContainText('삼성전자');
  await expect(page.getByTestId('hunter-shadow')).toContainText('1.5R 목표');
  await expect(page.getByTestId('hunter-shadow')).toContainText('Hermes APPROVE');
  await expect(page.getByTestId('hunter-shadow')).toContainText('2.14%');
  await expect(page.locator('#positions-body a').first()).toHaveAttribute(
    'href',
    'https://www.tossinvest.com/stocks/A005930/order',
  );

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
  await expect(page.getByRole('tab', { name: 'COMPARE' })).toBeVisible();
  await expect(page.getByRole('tab', { name: '1MIN' })).toBeVisible();
  await expect(page.getByText('READ ONLY')).toBeVisible();

  await page.goto('/cycles');
  await page.locator('#date-filter').selectOption('2026-08-13');
  await expect(page.locator('.cycle-universe')).toBeVisible();
  await page.locator('.cycle-universe').click();
  await expect(page.locator('.universe-list')).toContainText('삼성전자');
});

test('비교·판단·오류·1분봉 체결 마커를 함께 탐색한다', async ({ page }) => {
  await page.goto('/');
  await page.getByRole('button', { name: '이전 거래일' }).click();

  await page.getByRole('tab', { name: 'COMPARE' }).click();
  await expect(page.getByTestId('comparison-view')).toBeVisible();
  await expect(page.getByTestId('equity-chart').locator('polyline')).toHaveCount(2);
  await expect(page.locator('#compare-rule-holdings')).toContainText('삼성전자');
  await expect(page.locator('#compare-rule-holdings a')).toHaveAttribute(
    'href',
    'https://www.tossinvest.com/stocks/A005930/order',
  );
  await expect(page.locator('#compare-hermes-holdings')).toContainText('SK하이닉스');
  await expect(page.getByTestId('decision-log')).toContainText('변동성 정보가 부족합니다.');
  await expect(page.getByTestId('decision-log')).toContainText('거부');
  await expect(page.getByTestId('error-log')).toContainText('Hermes API timeout');

  await page.getByRole('tab', { name: '1MIN' }).click();
  await page.locator('#minute-symbol').selectOption('005930');
  await expect(page.getByTestId('minute-view')).toBeVisible();
  await expect(page.locator('#minute-chart rect')).toHaveCount(2);
  await expect(page.locator('#minute-chart polygon')).toHaveCount(1);
  await expect(page.locator('#minute-executions')).toContainText('RULE BUY');
  await expect(page.locator('#minute-executions')).toContainText('rule entry');
});

test('cycle 실행 결과와 종목별 차단 사유를 조회한다', async ({ page }) => {
  const response = await page.goto('/cycles');
  expect(response.status()).toBe(200);

  await expect(page.getByTestId('cycle-timeline')).toBeVisible();
  await page.locator('#date-filter').selectOption('2026-08-13');
  await expect(page.locator('.cycle-row')).toHaveCount(1);
  await expect(page.locator('.cycle-card.rule')).toContainText('성공');
  await expect(page.locator('.cycle-card.hermes')).toContainText('Hermes API timeout');
  await expect(page.locator('#signal-fill')).toHaveText('1 / 1');

  await expect(page.locator('.cycle-universe')).toContainText('UNIVERSE 1종목');
  await page.locator('.cycle-universe').click();
  await expect(page.locator('.universe-list')).toContainText('005930');
  await expect(page.locator('.universe-list')).toContainText('삼성전자');
  await expect(page.locator('.universe-trend polyline')).toHaveCount(1);
  await expect(page.locator('.universe-trend .universe-marker')).toHaveCount(1);
  await expect(page.locator('.universe-item')).toContainText('72,000');

  await page.locator('.cycle-card.rule details').click();
  await expect(page.locator('.cycle-card.rule .funnel')).toContainText('v2 차단 14');
  await expect(page.locator('.cycle-card.rule .symbol-state-list')).toContainText('삼성전자');
  await expect(page.locator('.cycle-card.rule .symbol-state-list')).toContainText('수급 반전 미확인');
});

test('Hermes 판단 rationale을 조회한다', async ({ page }) => {
  const response = await page.goto('/hermes');
  expect(response.status()).toBe(200);
  await expect(page.getByTestId('hermes-log')).toBeVisible();
  await expect(page.locator('.hermes-kind')).toContainText('종목 판단');
  await expect(page.locator('.hermes-body')).toContainText('위험 한도 안입니다.');
});
