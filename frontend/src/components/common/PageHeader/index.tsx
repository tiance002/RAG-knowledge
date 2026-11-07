/**
 * 页面头部组件
 */

import React from 'react';
import { Breadcrumb, Button } from 'antd';
import { ArrowLeftOutlined } from '@ant-design/icons';
import { useNavigate } from 'react-router-dom';
import styles from './PageHeader.module.css';

interface BreadcrumbItem {
  title: string;
  path?: string;
}

interface PageHeaderProps {
  title: string;
  subtitle?: string;
  subTitle?: string;
  className?: string;
  onBack?: () => void | Promise<void>;
  breadcrumbs?: BreadcrumbItem[];
  showBack?: boolean;
  extra?: React.ReactNode;
}

const PageHeader: React.FC<PageHeaderProps> = ({
  title,
  subtitle,
  subTitle,
  className,
  onBack,
  breadcrumbs,
  showBack = false,
  extra,
}) => {
  const navigate = useNavigate();

  const handleBack = () => {
    if (onBack) void onBack();
    else navigate(-1);
  };

  return (
    <div className={[styles.pageHeader, className].filter(Boolean).join(' ')}>
      {breadcrumbs && breadcrumbs.length > 0 && (
        <Breadcrumb
          className={styles.breadcrumb}
          items={breadcrumbs.map((item) => ({
            title: item.path ? (
              <a onClick={() => navigate(item.path!)}>{item.title}</a>
            ) : (
              item.title
            ),
          }))}
        />
      )}

      <div className={styles.titleRow}>
        <div className={styles.titleSection}>
          {(showBack || onBack) && (
            <Button
              type="text"
              icon={<ArrowLeftOutlined />}
              onClick={handleBack}
              className={styles.backButton}
            />
          )}
          <div>
            <h1 className={styles.title}>{title}</h1>
            {(subtitle || subTitle) && <p className={styles.subtitle}>{subtitle || subTitle}</p>}
          </div>
        </div>

        {extra && <div className={styles.extra}>{extra}</div>}
      </div>
    </div>
  );
};

export default PageHeader;
